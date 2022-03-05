from wacomponents.application import setup_app_environment

setup_app_environment(setup_kivy=False)

import os.path
from concurrent.futures.thread import ThreadPoolExecutor

import random
import time
from kivy.logger import Logger as logger
from uuid import UUID
from datetime import timedelta, datetime, timezone

from wacryptolib.cryptainer import CRYPTAINER_TRUSTEE_TYPES, SHARED_SECRET_ALGO_MARKER, LOCAL_KEYFACTORY_TRUSTEE_MARKER, \
    CryptainerStorage, ReadonlyCryptainerStorage
from wacryptolib.keystore import KeystoreBase
from wacryptolib.sensor import TarfileRecordAggregator, SensorManager
from wacryptolib.utilities import synchronized
from wacomponents.application.recorder_service import WaRecorderService
from wacomponents.logging.handlers import safe_catch_unhandled_exception
from wacomponents.utilities import get_system_information, convert_bytes_to_human_representation
from wacomponents.i18n import tr
try:
    from wacomponents.devices.gpio_buttons import register_button_callback
except ImportError:
    register_button_callback = lambda *args, **kwargs: None
from wanvr.common_runtime import WanvrRuntimeSupportMixin
from wacomponents.sensors.camera.rtsp_stream import RtspCameraSensor


# FIXME move this to wacryptolib
class PassthroughTarfileRecordAggregator(TarfileRecordAggregator):  #FIXME WRONG NAME

    @synchronized
    def add_record(self, sensor_name: str, from_datetime, to_datetime, extension: str, payload: bytes):

        filename = self._build_record_filename(
            sensor_name=sensor_name, from_datetime=from_datetime, to_datetime=to_datetime, extension=extension
        )
        self._cryptainer_storage.enqueue_file_for_encryption(
            filename_base=filename, payload=payload, metadata={}
        )

    @synchronized
    def finalize_tarfile(self):
        pass  # DO NOTHING



class WanvrBackgroundServer(WanvrRuntimeSupportMixin, WaRecorderService):  # FIXME RENAME THIS

    # CLASS VARIABLES #
    thread_pool_executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="service_worker"  # SINGLE worker for now, to avoid concurrency
    )

    _epaper_display = None  # Not always available

    def __init__(self):
        super().__init__()
        self._setup_epaper_screen()

    def _setup_epaper_screen(self):
        try:
            from wacomponents.devices.epaper import EpaperStatusDisplay
        except ImportError:
            logger.warning("Could not import EpaperStatusDisplay, aborting setup of epaper display")
            return
        logger.info("Setting up epaper screen and refresh/on-off buttons")
        self._epaper_display = EpaperStatusDisplay()
        register_button_callback(self._epaper_display.BUTTON_PIN_1, self._epaper_status_refresh_callback)
        register_button_callback(self._epaper_display.BUTTON_PIN_2, self._epaper_switch_recording_callback)

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
                last_cryptainer_str = "%s (%s)" % (_last_cryptainer_age_s, _last_cryptainer_size_str)

        try:
            preview_image_age_s = "%ss" % int(time.time() - os.path.getmtime(self.preview_image_path))
        except FileNotFoundError:
            pass

        status_obj.update({  # Maps will-be-labels to values
            "recording_status": "ON" if self.is_recording else "OFF",
            "container_count": cryptainers_count_str,
            "last_cryptainer": last_cryptainer_str,
            "last_thumbnail": preview_image_age_s
        })
        return status_obj

    @safe_catch_unhandled_exception
    def _epaper_status_refresh_callback(self, *args, **kwargs):  # Might receive pin number and such as arguments

        logger.info("Epaper status refresh callback was triggered")
        epaper_display = self._epaper_display
        assert epaper_display, epaper_display
        epaper_display.initialize_display()

        status_obj = self._retrieve_epaper_display_information()

        epaper_display.display_status(status_obj, preview_image_path=str(self.preview_image_path))
        epaper_display.release_display()

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
        for keystore_uid_str in selected_keystore_uids:
            keystore = filesystem_keystore_pool.get_foreign_keystore(keystore_uid=keystore_uid_str)
            key_information_list = keystore.list_keypair_identifiers()
            key = random.choice(key_information_list)

            shard_trustee = dict(
                trustee_type=CRYPTAINER_TRUSTEE_TYPES.AUTHENTICATOR_TRUSTEE,
                keystore_uid=UUID(keystore_uid_str)
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
        payload_signatures = [
                              dict(
                                  payload_digest_algo="SHA256",
                                  payload_signature_algo="DSA_DSS",
                                  payload_signature_trustee=LOCAL_KEYFACTORY_TRUSTEE_MARKER,
                                  keychain_uid=UUID("06c4ae77-abed-40d9-8adf-82c11261c8d6"),  # Arbitrary but FIXED!
                              )
                          ]
        payload_cipher_layers = [
            dict(
                 payload_cipher_algo="AES_CBC",
                 key_cipher_layers=shared_secret_encryption,
                 payload_signatures=payload_signatures)
        ]
        cryptoconf = dict(payload_cipher_layers=payload_cipher_layers)

        #print(">>>>> USING ENCRYPTION CONF")
        #import pprint ; pprint.pprint(cryptoconf)
        return cryptoconf

    def _build_recording_toolchain(self):

        #Was using rtsp://viewer:SomePwd8162@192.168.0.29:554/Streaming/Channels/101

        cryptainer_dir = self.get_cryptainer_dir()  # Might raise
        if not cryptainer_dir.is_dir():
            raise RuntimeError(f"Invalid containers dir setting: {cryptainer_dir}")

        #print(">>>>>>>>>>>>>>ENCRYPTION TO", containers_dir, "with max age", self.get_max_cryptainer_age_day())

        cryptainer_storage = CryptainerStorage(  # FIXME deduplicate paramaters with default (readonly) CryptainerStorage
                       default_cryptoconf=self._get_cryptoconf(),
                       cryptainer_dir=cryptainer_dir,
                       keystore_pool=self.filesystem_keystore_pool,
                       max_workers=1, # Protect memory usage
                       max_cryptainer_age=timedelta(days=self.get_max_cryptainer_age_day()))

        assert cryptainer_storage is not None, cryptainer_storage

        ip_camera_url = self.get_ip_camera_url()  #FIXME normalize names

        rtsp_camera_sensor = RtspCameraSensor(
                interval_s=self.get_video_recording_duration_mn()*60,
                cryptainer_storage=cryptainer_storage,
                video_stream_url=ip_camera_url,
                preview_image_path=self.preview_image_path)

        sensors_manager = SensorManager(sensors=[rtsp_camera_sensor])
 
        toolchain = dict(
            sensors_manager=sensors_manager,
            data_aggregators=[],
            tarfile_aggregators=[],
            cryptainer_storage=cryptainer_storage,
            free_keys_generator_worker=None,  # For now
        )
        return toolchain


def main():
    logger.info("Service process launches")
    server = WanvrBackgroundServer()
    server.join()
    logger.info("Service process exits")