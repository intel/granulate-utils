#
# Copyright (C) 2023 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from typing import List

import setuptools


def read_requirements(path: str) -> List[str]:
    with open(path) as f:
        return [line for line in f.readlines() if not line.startswith("#")]


with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setuptools.setup(
    name="granulate_utils",
    author="Granulate",
    author_email="",  # TODO
    description="Granulate Python utilities",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/Granulate/granulate-utils",
    classifiers=[
        "Programming Language :: Python :: 3",
    ],
    packages=setuptools.find_packages(),
    package_data={"granulate_utils": ["py.typed"], "glogger": ["py.typed"]},
    include_package_data=True,
    install_requires=read_requirements("requirements.txt"),
    python_requires=">=3.10",
    setup_requires=["setuptools-git-versioning<2"],
    setuptools_git_versioning={
        "enabled": True,
    },
)
