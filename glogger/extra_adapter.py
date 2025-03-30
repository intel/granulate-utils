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
import logging
import sys
from typing import Any, Dict, Mapping, MutableMapping, Tuple


class ExtraAdapter(logging.LoggerAdapter):
    """
    This adapter:
    1. Allows enriching messages with static and dynamic extra attributes, and
    2. Adds an attribute named "extra" to each record that contains all the extra attributes.
    """

    logging_kwargs = {"exc_info", "stack_info", "stacklevel", "extra"}

    def __init__(self, logger: logging.Logger, extra: Mapping[str, Any] = None):
        # If default extra not provided, use empty dict:
        super().__init__(logger, extra=extra or {})

    def get_extra(self, **kwargs) -> Mapping[str, Any]:
        """
        Get extra attributes for a message. kwargs contains all the kwargs passed to the logging call, with any
        non-standard kwargs available under an "extra" key. Default implementation merges existing attributes with
        default ones provided at initialization.
        """
        if self.extra is not None:
            return {**self.extra, **kwargs.get("extra", {})}
        return kwargs.get("extra", {})

    def process(self, msg: Any, kwargs: MutableMapping[str, Any]) -> Tuple[Any, MutableMapping[str, Any]]:
        # Partition the kwargs into logging kwargs and extra kwargs:
        logging_kwargs: Dict[str, Any] = {}
        other_kwargs = {}
        for k, v in kwargs.items():
            if k in self.logging_kwargs:
                logging_kwargs[k] = v
            else:
                other_kwargs[k] = v

        # Merge other kwargs into extra:
        extra: Mapping[str, Any] = {**logging_kwargs.get("extra", {}), **other_kwargs}
        if extra:
            logging_kwargs["extra"] = extra

        extra = self.get_extra(**logging_kwargs)

        if logging_kwargs.get("exc_info") is True and (exc_info := sys.exc_info()) is not None:
            # If exc_info is True, and the exception has a dict in the 'extra' attribute, merge it into extra:
            if (exc_extra := getattr(exc_info[1], "extra", None)) is not None and isinstance(exc_extra, dict):
                # Merge 'extra' attributes from exception into extra, the exception's extra attributes take precedence:
                extra = {**extra, **exc_extra}

        # Retain all extras as attributes on the record, and add "extra" attribute that contains all the extras:
        logging_kwargs.update({"extra": {**extra, "extra": extra}})
        return msg, logging_kwargs
