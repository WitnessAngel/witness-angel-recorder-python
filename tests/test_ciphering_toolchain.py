import pytest
import random
import uuid
from client.ciphering_toolchain import encrypt_video_stream, decrypt_video_stream

from wacryptolib.container import LOCAL_ESCROW_PLACEHOLDER

SIMPLE_SHAMIR_CONTAINER_CONF = dict(
    data_encryption_strata=[
        dict(
            data_encryption_algo="AES_CBC",
            key_encryption_strata=[
                dict(
                    key_encryption_algo="RSA_OAEP", key_escrow=LOCAL_ESCROW_PLACEHOLDER
                ),
                dict(
                    key_encryption_algo="SHARED_SECRET",
                    key_shared_secret_threshold=3,
                    key_shared_secret_escrows=[
                        dict(
                            shard_encryption_algo="RSA_OAEP",
                            # shared_escrow=dict(url="http://example.com/jsonrpc"),
                            shard_escrow=LOCAL_ESCROW_PLACEHOLDER,
                        ),
                        dict(
                            shard_encryption_algo="RSA_OAEP",
                            # shared_escrow=dict(url="http://example.com/jsonrpc"),
                            shard_escrow=LOCAL_ESCROW_PLACEHOLDER,
                        ),
                        dict(
                            shard_encryption_algo="RSA_OAEP",
                            # shared_escrow=dict(url="http://example.com/jsonrpc"),
                            shard_escrow=LOCAL_ESCROW_PLACEHOLDER,
                        ),
                        dict(
                            shard_encryption_algo="RSA_OAEP",
                            # shared_escrow=dict(url="http://example.com/jsonrpc"),
                            shard_escrow=LOCAL_ESCROW_PLACEHOLDER,
                        ),
                        dict(
                            shard_encryption_algo="RSA_OAEP",
                            # shared_escrow=dict(url="http://example.com/jsonrpc"),
                            shard_escrow=LOCAL_ESCROW_PLACEHOLDER,
                        ),
                    ],
                ),
            ],
            data_signatures=[
                dict(
                    message_prehash_algo="SHA256",
                    signature_algo="DSA_DSS",
                    signature_escrow=LOCAL_ESCROW_PLACEHOLDER,
                )
            ],
        )
    ]
)

COMPLEX_SHAMIR_CONTAINER_CONF = dict(
    data_encryption_strata=[
        dict(
            data_encryption_algo="AES_EAX",
            key_encryption_strata=[
                dict(
                    key_encryption_algo="RSA_OAEP", key_escrow=LOCAL_ESCROW_PLACEHOLDER
                )
            ],
            data_signatures=[],
        ),
        dict(
            data_encryption_algo="AES_CBC",
            key_encryption_strata=[
                dict(
                    key_encryption_algo="RSA_OAEP", key_escrow=LOCAL_ESCROW_PLACEHOLDER
                )
            ],
            data_signatures=[
                dict(
                    message_prehash_algo="SHA3_512",
                    signature_algo="DSA_DSS",
                    signature_escrow=LOCAL_ESCROW_PLACEHOLDER,
                )
            ],
        ),
        dict(
            data_encryption_algo="CHACHA20_POLY1305",
            key_encryption_strata=[
                dict(
                    key_encryption_algo="SHARED_SECRET",
                    key_shared_secret_threshold=2,
                    key_shared_secret_escrows=[
                        dict(
                            shard_encryption_algo="RSA_OAEP",
                            # shared_escrow=dict(url="http://example.com/jsonrpc"),
                            shard_escrow=LOCAL_ESCROW_PLACEHOLDER,
                        ),
                        dict(
                            shard_encryption_algo="RSA_OAEP",
                            # shared_escrow=dict(url="http://example.com/jsonrpc"),
                            shard_escrow=LOCAL_ESCROW_PLACEHOLDER,
                        ),
                        dict(
                            shard_encryption_algo="RSA_OAEP",
                            # shared_escrow=dict(url="http://example.com/jsonrpc"),
                            shard_escrow=LOCAL_ESCROW_PLACEHOLDER,
                        ),
                        dict(
                            shard_encryption_algo="RSA_OAEP",
                            # shared_escrow=dict(url="http://example.com/jsonrpc"),
                            shard_escrow=LOCAL_ESCROW_PLACEHOLDER,
                        ),
                    ],
                ),
                # dict(
                #     key_encryption_algo="RSA_OAEP", key_escrow=LOCAL_ESCROW_PLACEHOLDER
                # ),
            ],
            data_signatures=[
                dict(
                    message_prehash_algo="SHA3_256",
                    signature_algo="RSA_PSS",
                    signature_escrow=LOCAL_ESCROW_PLACEHOLDER,
                ),
                dict(
                    message_prehash_algo="SHA512",
                    signature_algo="ECC_DSS",
                    signature_escrow=LOCAL_ESCROW_PLACEHOLDER,
                ),
            ],
        ),
    ]
)


@pytest.mark.parametrize(
    "container_conf", [SIMPLE_SHAMIR_CONTAINER_CONF, COMPLEX_SHAMIR_CONTAINER_CONF]
)
def test_encrypt_video_stream(container_conf):
    path = "saved_video_stream/outpy.avi"
    metadata = random.choice([None, dict(a=[123])])
    keychain_uid = random.choice(
        [None, uuid.UUID("450fc293-b702-42d3-ae65-e9cc58e5a62a")]
    )

    ciphered_data = encrypt_video_stream(
        path=path, conf=container_conf, keychain_uid=keychain_uid, metadata=metadata
    )
    assert isinstance(ciphered_data, dict)

    result_data = decrypt_video_stream(container=ciphered_data)
    assert isinstance(result_data, bytes)
    with open(path, "rb") as video_stream:
        data = video_stream.read()

    assert result_data == data