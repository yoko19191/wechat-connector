"""Compatibility alias for callers of the original module."""
import sys
from wechat_connector import cipher_db as _module
sys.modules[__name__] = _module
