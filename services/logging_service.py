#!/usr/bin/env python3
"""Structured logging helpers for application events."""

from __future__ import annotations

import json
import time


def log_event(logger, event_type: str, **payload):
    record = {
        "event_type": event_type,
        "timestamp": int(time.time()),
        **payload,
    }
    logger.info(json.dumps(record, sort_keys=True))


def log_warning(logger, event_type: str, **payload):
    record = {
        "event_type": event_type,
        "timestamp": int(time.time()),
        **payload,
    }
    logger.warning(json.dumps(record, sort_keys=True))
