import logging
import os.path
import threading
from concurrent.futures.thread import ThreadPoolExecutor

import random
import time
from uuid import UUID
from datetime import timedelta, datetime, timezone

from wacomponents.default_settings import IS_RASPBERRY_PI
from wacomponents.devices.epaper import get_epaper_instance, EPAPER_TYPES
from wacomponents.devices.lcd import get_lcd_instance
from wacomponents.sensors.camera.raspberrypi_camera_microphone import RaspberryLibcameraSensor, \
    RaspberryAlsaMicrophoneSensor, list_pulseaudio_microphone_names, is_legacy_rpi_camera_enabled, \
    RaspberryRaspividSensor, RaspberryPicameraSensor
from wacryptolib.cryptainer import CRYPTAINER_TRUSTEE_TYPES, SHARED_SECRET_ALGO_MARKER, \
    CryptainerStorage, ReadonlyCryptainerStorage, check_cryptoconf_sanity
from wacryptolib.keystore import KeystoreBase
from wacryptolib.sensor import TarfileRecordAggregator, SensorManager
from wacryptolib.utilities import synchronized
from wacomponents.application.recorder_service import WaRecorderService, ActivityNotificationType
from wacomponents.logging.handlers import safe_catch_unhandled_exception
from wacomponents.utilities import get_system_information, convert_bytes_to_human_representation, \
    MONOTHREAD_POOL_EXECUTOR
from wacomponents.i18n import tr
try:
    from wacomponents.devices.gpio_buttons import register_button_callback
except ImportError:
    register_button_callback = lambda *args, **kwargs: None
from wanvr.common_runtime import WanvrRuntimeSupportMixin
from wacomponents.sensors.camera.rtsp_stream import RtspCameraSensor


logger = logging.getLogger(__name__)


class WanvrBackgroundServer(WanvrRuntimeSupportMixin, WaRecorderService):  # FIXME RENAME THIS

    _epaper_display = None  # Not always available
    _lcd_display = None  # Not always available

    _led_callback = None  # Can be set to a function taking 0-255 color tuple (R,G,B) as argument

    _lock = threading.Lock()  # At class level is OK here

    def __init__(self):
        super().__init__()
        self._setup_peripherals()

    def _setup_peripherals(self):

        if not IS_RASPBERRY_PI:
            return  # No special display/buttons on PC

        recording_switch_pin = None
        epaper_status_refresh_pin = None

        epaper_type = self.get_epaper_type()
        if epaper_type:
            try:
                epaper_display = get_epaper_instance(epaper_type)
            except (ImportError, ValueError):
                logger.warning("Could not import E-PAPER display of type %r, aborting" % epaper_type)
            else:
                recording_switch_pin = epaper_display.BUTTON_PIN_1
                epaper_status_refresh_pin = epaper_display.BUTTON_PIN_2
                self._epaper_display = epaper_display

        lcd_type = self.get_lcd_type()
        if lcd_type:
            try:
                lcd_display = get_lcd_instance(lcd_type)
            except (ImportError, ValueError):
                logger.warning("Could not import LCD display of type %r, aborting" % lcd_type)
                raise
            else:
                recording_switch_pin = lcd_display.BUTTON_PIN_1
                # No status display with LCD for now
                self._lcd_display = lcd_display

        logger.info("Setting up display and refresh/on-off buttons")
        if recording_switch_pin is not None:
            register_button_callback(recording_switch_pin, self._epaper_switch_recording_callback)
        if epaper_status_refresh_pin is not None:
            assert self._epaper_display
            register_button_callback(epaper_status_refresh_pin, self._epaper_status_refresh_callback)

        if self.get_enable_button_shim():
            logger.info("Setting up buttonshim device")
            import buttonshim  # Smbus-based driver for Raspberry
            # This lib automatically cleans up thanks to atexit
            buttonshim.on_press(buttonshim.BUTTON_A, self._epaper_switch_recording_callback)
            if self._epaper_display:
                buttonshim.on_press(buttonshim.BUTTON_B, self._epaper_status_refresh_callback)
            buttonshim.set_brightness(0.2)

            def _buttonshim_led_callback(color):
                buttonshim.set_pixel(*color)
                time.sleep(0.1)  # FIXME problematic sleep(), as it blocks the recording thread...
                buttonshim.set_pixel(0, 0, 0)  # Turn LED off

            self._led_callback = _buttonshim_led_callback
            self._led_callback((100, 40, 40))

    def _dispatch_activity_notification(self, notification_type,
                                        notification_color=None, notification_image=None):
        executor = MONOTHREAD_POOL_EXECUTOR.submit
        assert getattr(ActivityNotificationType, notification_type), notification_type
        if notification_type == ActivityNotificationType.RECORDING_PROGRESS:
            assert notification_color
            executor(lambda: self._blink_on_recording(notification_color))
        else:
            print(">>>>>>>>>>>>> _dispatch_activity_notification image preview")
            assert notification_type == ActivityNotificationType.IMAGE_PREVIEW
            pass ####

    @synchronized
    def _blink_on_recording(self, notification_color):
        print(">>>>>>>>>>>>>>>>> _blink_on_recording CALLED for", self._led_callback)
        if self._led_callback:
            self._led_callback(notification_color)

    def _retrieve_epaper_display_information(self):

        status_obj = get_system_information(self.get_cryptainer_dir())

        cryptainers_count_str = last_cryptainer_str = preview_image_age_s =  tr._("N/A")

        readonly_cryptainer_storage: ReadonlyCryptainerStorage = self.get_cryptainer_storage_or_none(read_only=True)

        if readonly_cryptainer_storage:
            cryptainer_names = readonly_cryptainer_storage.list_cryptainer_names(as_sorted_list=True)
            cryptainers_count_str = str(len(cryptainer_names))
            if cryptainer_names:
                _last_cryptainer_name = cryptainer_names[-1]  # We consider that their names contain proper timestamps
                _last_cryptainer_size_str = convert_bytes_to_human_representation(readonly_cryptainer_storage._get_cryptainer_size(_last_cryptainer_name))
                _utcnow = datetime.utcnow().replace(tzinfo=timezone.utc)
                _last_cryptainer_age_s = "%ds" % (_utcnow - readonly_cryptainer_storage._get_cryptainer_datetime_utc(_last_cryptainer_name)).total_seconds()
                last_cryptainer_str = "%s  (%s)" % (_last_cryptainer_age_s, _last_cryptainer_size_str)

        try:
            preview_image_age_s = "%ss" % int(time.time() - os.path.getmtime(self.preview_image_path))
        except FileNotFoundError:
            pass

        status_obj.update({  # Maps will-be-labels to values
            "recording_status": "ON" if self.is_recording else "OFF",
            "container_count": cryptainers_count_str,
            "last_container": last_cryptainer_str,
            "last_thumbnail": preview_image_age_s
        })

        # Put fields in importance order
        sorted_field_names = [
            "recording_status", "wifi_status", "ethernet_status", "now_datetime",
            "last_thumbnail", "last_container", "container_count",
            "disk_left", "ram_left",
        ]
        assert len(sorted_field_names) == len(status_obj)
        status_obj_sorted = {
            field_name: status_obj[field_name]
            for field_name in sorted_field_names
        }
        #print("status_obj_sorted>>>>>", status_obj_sorted)
        return status_obj_sorted

    @safe_catch_unhandled_exception
    @synchronized
    def _epaper_status_refresh_callback(self, *args, **kwargs):  # Might receive pin number and such as arguments

        logger.info("Epaper status refresh callback was triggered")
        epaper_display = self._epaper_display
        assert epaper_display, epaper_display
        epaper_display.initialize_display()

        try:
            status_obj = self._retrieve_epaper_display_information()
            epaper_display.display_status(status_obj, preview_image_path=str(self.preview_image_path))
        finally:
            epaper_display.release_display()  # Important to avoid harming the screen

    @synchronized
    def _epaper_switch_recording_callback(self, *args, **kwargs):  # Might receive pin number and such as arguments
        logger.info("Epaper recording switch callback  was triggered")
        if self.is_recording:
            self.stop_recording()
        else:
            self.start_recording()

    def _get_cryptoconf(self):
        """Return a wacryptolib-compatible encryption configuration"""
        keyguardian_threshold = self.get_keyguardian_threshold()
        selected_keystore_uids = self._load_selected_keystore_uids()
        return self._build_cryptoconf(
                keyguardian_threshold=keyguardian_threshold,
                selected_keystore_uids=selected_keystore_uids,
                filesystem_keystore_pool=self.filesystem_keystore_pool)

    @staticmethod
    def _build_cryptoconf(keyguardian_threshold: int,
                               selected_keystore_uids: list,
                               filesystem_keystore_pool: KeystoreBase):
        info_trustees = []

        all_foreign_keystore_metadata = filesystem_keystore_pool.get_all_foreign_keystore_metadata()

        for keystore_uid_str in selected_keystore_uids:
            keystore_uid = UUID(keystore_uid_str)
            keystore_owner = all_foreign_keystore_metadata[keystore_uid]["keystore_owner"]  # Shall exist
            keystore = filesystem_keystore_pool.get_foreign_keystore(keystore_uid=keystore_uid)
            key_information_list = keystore.list_keypair_identifiers()
            key = random.choice(key_information_list)  # IMPORTANT - randomly select a key

            shard_trustee = dict(
                trustee_type=CRYPTAINER_TRUSTEE_TYPES.AUTHENTICATOR_TRUSTEE,
                keystore_uid=keystore_uid,
                keystore_owner=keystore_owner,
            )

            info_trustees.append(
                dict(key_cipher_layers=[dict(
                    key_cipher_algo=key["key_algo"],
                    keychain_uid=key["keychain_uid"],
                    key_cipher_trustee=shard_trustee,
                 )])
            )
        shared_secret_encryption = [
                                      dict(
                                         key_cipher_algo=SHARED_SECRET_ALGO_MARKER,
                                         key_shared_secret_threshold=keyguardian_threshold,
                                         key_shared_secret_shards=info_trustees,
                                      )
                                   ]
        payload_signatures = []
        payload_cipher_layers = [
            dict(
                 payload_cipher_algo="AES_CBC",
                 key_cipher_layers=shared_secret_encryption,
                 payload_signatures=payload_signatures)
        ]
        cryptoconf = dict(payload_cipher_layers=payload_cipher_layers)
        check_cryptoconf_sanity(cryptoconf)  # Sanity check
        #print(">>>>> USING ENCRYPTION CONF")
        #import pprint ; pprint.pprint(cryptoconf)
        return cryptoconf

    def _build_recording_toolchain(self):

        #Was using rtsp://viewer:SomePwd8162@192.168.0.29:554/Streaming/Channels/101

        cryptainer_dir = self.get_cryptainer_dir()  # Might raise
        if not cryptainer_dir.is_dir():
            raise RuntimeError(f"Invalid containers dir setting: {cryptainer_dir}")

        #print(">>>>>>>>>>>>>>ENCRYPTION TO", containers_dir, "with max age", self.get_max_cryptainer_age_day())

        cryptainer_storage = CryptainerStorage(
                       default_cryptoconf=self._get_cryptoconf(),
                       cryptainer_dir=cryptainer_dir,
                       keystore_pool=self.filesystem_keystore_pool,
                       max_workers=1, # Protect memory usage
                       max_cryptainer_age=timedelta(days=self.get_max_cryptainer_age_day()))

        assert cryptainer_storage is not None, cryptainer_storage

        sensors = []
        recording_duration_s = self.get_recording_duration_mn()*60

        enable_ip_camera = self.get_enable_ip_camera()
        ip_camera_url = self.get_ip_camera_url()
        ffmpeg_rtsp_parameters = self.get_ffmpeg_rtsp_parameters()
        ffmpeg_rtsp_output_format = self.get_ffmpeg_rtsp_output_format()
        if enable_ip_camera:
            rtsp_camera_sensor = RtspCameraSensor(
                interval_s=recording_duration_s,
                cryptainer_storage=cryptainer_storage,
                video_stream_url=ip_camera_url,
                preview_image_path=self.preview_image_path,
                ffmpeg_rtsp_parameters=ffmpeg_rtsp_parameters,
                ffmpeg_rtsp_output_format=ffmpeg_rtsp_output_format,
                activity_notification_callback=self._dispatch_activity_notification)
            sensors.append(rtsp_camera_sensor)

        if IS_RASPBERRY_PI:

            legacy_rpi_camera_enabled = is_legacy_rpi_camera_enabled()

            enable_local_camera = self.get_enable_local_camera()
            enable_local_microphone = self.get_enable_local_microphone()
            compress_local_microphone_recording = self.get_compress_local_microphone_recording()
            enable_local_camera_microphone_muxing = self.get_enable_local_camera_microphone_muxing()

            _audio_is_already_handled = False

            if enable_local_camera:

                if legacy_rpi_camera_enabled:

                    if False:
                        logging.warning("Using LEGACY raspivid sensor")

                        raspivid_parameters = self.get_raspivid_parameters()

                        raspberry_raspivid_sensor = RaspberryRaspividSensor(
                            interval_s=recording_duration_s,
                            cryptainer_storage=cryptainer_storage,
                            preview_image_path=self.preview_image_path,
                            raspivid_parameters=raspivid_parameters,
                            activity_notification_callback=self._dispatch_activity_notification,
                        )

                        sensors.append(raspberry_raspivid_sensor)

                    else:
                        logging.warning("Using LEGACY picamera sensor")

                        picamera_parameters = self.get_picamera_parameters()

                        raspberry_picamera_sensor = RaspberryPicameraSensor(
                            interval_s=recording_duration_s,
                            cryptainer_storage=cryptainer_storage,
                            preview_image_path=self.preview_image_path,
                            picamera_parameters=picamera_parameters,
                            activity_notification_callback=self._dispatch_activity_notification,
                        )

                        sensors.append(raspberry_picamera_sensor)


                else:

                    logging.warning("Using MODERN libcamera sensor")  # Broken on raspberry pi zero V1, not enough power...

                    alsa_device_name = None
                    if enable_local_camera_microphone_muxing and enable_local_microphone:
                        alsa_device_name = list_pulseaudio_microphone_names()[0]
                        _audio_is_already_handled = True

                    libcameravid_video_parameters = self.get_libcameravid_video_parameters()
                    libcameravid_audio_parameters = self.get_libcameravid_audio_parameters()

                    raspberry_libcamera_sensor = RaspberryLibcameraSensor(
                        interval_s=recording_duration_s,
                        cryptainer_storage=cryptainer_storage,
                        preview_image_path=self.preview_image_path,
                        alsa_device_name=alsa_device_name,
                        libcameravid_video_parameters=libcameravid_video_parameters,
                        libcameravid_audio_parameters=libcameravid_audio_parameters,
                        # No activity_notification_callback for now, has to be integrated and tests
                    )
                    sensors.append(raspberry_libcamera_sensor)

            if enable_local_microphone and not _audio_is_already_handled:  # Separate audio recording

                arecord_parameters = self.get_arecord_parameters()
                arecord_output_format = self.get_arecord_output_format()
                ffmpeg_alsa_parameters = self.get_ffmpeg_alsa_parameters()
                ffmpeg_alsa_output_format = self.get_ffmpeg_alsa_output_format()

                alsa_microphone_sensor = RaspberryAlsaMicrophoneSensor(
                    interval_s=recording_duration_s,
                    cryptainer_storage=cryptainer_storage,
                    compress_recording=compress_local_microphone_recording,
                    arecord_parameters=arecord_parameters,
                    arecord_output_format=arecord_output_format,
                    ffmpeg_alsa_parameters=ffmpeg_alsa_parameters,
                    ffmpeg_alsa_output_format=ffmpeg_alsa_output_format,
                    activity_notification_callback=self._dispatch_activity_notification,
                )
                sensors.append(alsa_microphone_sensor)

        assert sensors, sensors  # Conf checkers should ensure this
        sensors_manager = SensorManager(sensors=sensors)

        toolchain = dict(
            sensors_manager=sensors_manager,
            data_aggregators=[],
            tarfile_aggregators=[],
            cryptainer_storage=cryptainer_storage,
            free_keys_generator_worker=None,  # For now, no pregeneration of local keys
        )
        return toolchain


def main():
    logger.info("Service process launches")
    server = WanvrBackgroundServer()

    #import logging_tree; logging_tree.printout()
    #print(">>>>> TRIGGER FORCE REFRESH CALLBACK OF EPAPE SOON")
    #time.sleep(1)
    #server._epaper_status_refresh_callback()

    server.join()
    logger.info("Service process exits")
