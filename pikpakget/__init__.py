"""PikpakGet — sequential PikPak share-link grabber for small cloud quotas."""
from .api import Client, PikPakError, Session, captcha_sign, parse_share_url
from .pipeline import Pipeline, State, human, read_links, safe_name
from .stream import download_segments, download_stream, plan_segments

__version__ = '0.1.1'
__all__ = ['Client', 'PikPakError', 'Session', 'captcha_sign', 'parse_share_url',
           'Pipeline', 'State', 'human', 'read_links', 'safe_name',
           'download_segments', 'download_stream', 'plan_segments', '__version__']
