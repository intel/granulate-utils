from typing import Union

import pytest
from packaging.version import Version

from granulate_utils.java import JvmVersion, parse_jvm_version


@pytest.mark.parametrize(
    "java_version,jvm_version_or_err",
    [
        (
            """openjdk version "1.8.0_265"
OpenJDK Runtime Environment (AdoptOpenJDK)(build 1.8.0_265-b01)
OpenJDK 64-Bit Server VM (AdoptOpenJDK)(build 25.265-b01, mixed mode)
""",
            JvmVersion(Version("8.265"), 1, "OpenJDK 64-Bit Server VM", "HotSpot", None),
        ),
        (
            """java version "18.0.0.1" 2022-05-19
Java(TM) SE Runtime Environment (build 18.0.0.1+2-9)
Java HotSpot(TM) 64-Bit Server VM (build 18.0.0.1+2-9, mixed mode, sharing)
""",
            JvmVersion(Version("18.0.0.1"), 2, "Java HotSpot(TM) 64-Bit Server VM", "HotSpot", None),
        ),
        (
            """openjdk version "1.8.0_125"
OpenJDK Runtime Environment (Temurin)(build 1.8.0_125-b09)
OpenJDK 64-Bit Server VM (Temurin)(build 25.125-b09, mixed mode)
""",
            JvmVersion(Version("8.125"), 9, "OpenJDK 64-Bit Server VM", "HotSpot", None),
        ),
        # 8
        (
            """openjdk version "1.8.0_352"
OpenJDK Runtime Environment (build 1.8.0_352-8u352-ga-1~22.04-b08)
OpenJDK 64-Bit Server VM (build 25.352-b08, mixed mode)
""",
            JvmVersion(Version("8.352"), 8, "OpenJDK 64-Bit Server VM", "HotSpot", None),
        ),
        # -ojdkbuild suffix
        (
            """openjdk version "1.8.0_201-ojdkbuild"
OpenJDK Runtime Environment (build 1.8.0_201-ojdkbuild-09)
OpenJDK 64-Bit Server VM (build 25.201-b09, mixed mode)""",
            JvmVersion(Version("8.201"), 9, "OpenJDK 64-Bit Server VM", "HotSpot", None),
        ),
        # -internal suffix
        (
            """openjdk version "1.8.0_111-internal"
OpenJDK Runtime Environment (build 1.8.0_111-internal-alpine-r0-b14)
OpenJDK 64-Bit Server VM (build 25.111-b14, mixed mode)""",
            JvmVersion(Version("8.111"), 14, "OpenJDK 64-Bit Server VM", "HotSpot", None),
        ),
        # -ea suffix
        (
            """openjdk version "13-ea" 2019-09-17
OpenJDK Runtime Environment (build 13-ea+32)
OpenJDK 64-Bit Server VM (build 13-ea+32, mixed mode, sharing)""",
            JvmVersion(Version("13"), 32, "OpenJDK 64-Bit Server VM", "HotSpot", None),
        ),
        # -b appears twice
        (
            """openjdk version "1.8.0_342"
OpenJDK Runtime Environment (build 1.8.0_342-8u342-b07-0ubuntu1~18.04-b07)
OpenJDK 64-Bit Server VM (build 25.342-b07, mixed mode)""",
            JvmVersion(Version("8.342"), 7, "OpenJDK 64-Bit Server VM", "HotSpot", None),
        ),
        # 17
        (
            """openjdk version "17.0.1" 2021-10-19
OpenJDK Runtime Environment (build 17.0.1+12-39)
OpenJDK 64-Bit Server VM (build 17.0.1+12-39, mixed mode, sharing)""",
            JvmVersion(Version("17.0.1"), 12, "OpenJDK 64-Bit Server VM", "HotSpot", None),
        ),
        # 11
        (
            """openjdk version "11.0.12" 2021-07-20
OpenJDK Runtime Environment (build 11.0.12+7-post-Debian-2deb10u1)
OpenJDK 64-Bit Server VM (build 11.0.12+7-post-Debian-2deb10u1, mixed mode)""",
            JvmVersion(Version("11.0.12"), 7, "OpenJDK 64-Bit Server VM", "HotSpot", None),
        ),
        # 7
        (
            """java version "1.7.0_251"
OpenJDK Runtime Environment (amzn-2.6.21.0.82.amzn1-x86_64 u251-b02)
OpenJDK 64-Bit Server VM (build 24.251-b02, mixed mode)""",
            JvmVersion(Version("7.251"), 2, "OpenJDK 64-Bit Server VM", "HotSpot", None),
        ),
        # zing 21
        (
            """java version "1.8.0_312"
Java Runtime Environment (Zing 21.12.0.0-b2-linux64) (build 1.8.0_312-b2)
Zing 64-Bit Tiered VM (Zing 21.12.0.0-b2-linux64) (build 1.8.0_312-zing_21.12.0.0-b3-product-linux-X86_64, mixed mode)""",  # noqa
            JvmVersion(
                Version("8.312"), 2, "Zing 64-Bit Tiered VM (Zing 21.12.0.0-b2-linux64)", "Zing", Version("21.12.0")
            ),
        ),
        # zing 22 & java 15
        (
            """java version "15.0.8" 2022-08-04
Java Runtime Environment Zing22.07.1.0+1 (build 15.0.8+4-MTS)
Zing 64-Bit Tiered VM Zing22.07.1.0+1 (build 15.0.8-zing_22.07.1.0-b1-product-linux-X86_64, mixed mode)""",
            JvmVersion(Version("15.0.8"), 4, "Zing 64-Bit Tiered VM Zing22.07.1.0+1", "Zing", Version("22.07.1")),
        ),
        # open j9
        (
            """openjdk version "1.8.0_332"
IBM Semeru Runtime Open Edition (build 1.8.0_332-b09)
Eclipse OpenJ9 VM (build openj9-0.32.0, JRE 1.8.0 Linux amd64-64-Bit Compressed References 20220422_370 (JIT enabled, AOT enabled)"""  # noqa
            """OpenJ9   - 9a84ec34e
OMR      - ab24b6666
JCL      - 0b8b8af39a based on jdk8u332-b09)
""",
            JvmVersion(Version("8.332"), 9, "Eclipse OpenJ9 VM", "OpenJ9", None),
        ),
        # zing 20 (zing-jdk1.8.0-20.03.0.0-1 on CentOS)
        (
            """java version "1.8.0-zing_20.03.0.0"
Zing Runtime Environment for Java Applications (build 1.8.0-zing_20.03.0.0-b1)
Zing 64-Bit Tiered VM (build 1.8.0-zing_20.03.0.0-b1-product-linux-X86_64, mixed mode)
""",
            JvmVersion(Version("8"), 1, "Zing 64-Bit Tiered VM", "Zing", Version("20.03.0")),
        ),
        # zing 19 (zing-jdk1.8.0-19.12.103.0-3 on CentOS)
        (
            """java version "1.8.0-zing_19.12.103.0"
Zing Runtime Environment for Java Applications (build 1.8.0-zing_19.12.103.0-b3)
Zing 64-Bit Tiered VM (build 1.8.0-zing_19.12.103.0-b3-product-linux-X86_64, mixed mode)
""",
            JvmVersion(Version("8"), 3, "Zing 64-Bit Tiered VM", "Zing", Version("19.12.103")),
        ),
        # zing 20 & java 11 (zing-jdk11.0.0-20.02.201.0-1 on CentOS)
        (
            """java version "11.0.7.0.101" 2020-06-03 LTS
Zing Runtime Environment for Java Applications 20.02.201.0+1 (product build 11.0.7.0.101+10-LTS)
Zing 64-Bit Tiered VM 20.02.201.0+1 (product build 11.0.7-zing_20.02.201.0-b1-product-linux-X86_64, mixed mode)""",
            JvmVersion(
                Version("11.0.7.0.101"), 10, "Zing 64-Bit Tiered VM 20.02.201.0+1", "Zing", Version("20.02.201")
            ),
        ),
        # TODO: add error cases here
    ],
)
def test_parse_jvm_version(java_version: str, jvm_version_or_err: Union[JvmVersion, str]) -> None:
    if isinstance(jvm_version_or_err, JvmVersion):
        assert parse_jvm_version(java_version) == jvm_version_or_err
    else:
        assert isinstance(jvm_version_or_err, str)
        with pytest.raises(Exception) as e:
            parse_jvm_version(java_version)
        assert e.value.args[0] == jvm_version_or_err
