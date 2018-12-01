
import calendar
import math
import re
import time

import gtts
import requests
import urllib3
from gtts.tts import log, gTTSError, _len

from .proxy import proxies


# TODO: Следить за актуальностью копипаст


# part of https://github.com/Boudewijn26/gTTS-token/blob/master/gtts_token/gtts_token.py#L51
def _get_token_key(self):
    if self.token_key is not None:
        return self.token_key

    # response = requests.get("https://translate.google.com/")
    response = requests.get("https://translate.google.com/", proxies=proxies('token_google'))
    tkk_expr = re.search("(tkk:.*?),", response.text)
    if not tkk_expr:
        raise ValueError(
            "Unable to find token seed! Did https://translate.google.com change?"
        )
    tkk_expr = tkk_expr.group(1)

    try:
        # Grab the token directly if already generated by function call
        result = re.search("\d{6}\.[0-9]+", tkk_expr).group(0)
    except AttributeError:
        # Generate the token using algorithm
        timestamp = calendar.timegm(time.gmtime())
        hours = int(math.floor(timestamp / 3600))
        a = re.search("a\\\\x3d(-?\d+);", tkk_expr).group(1)
        b = re.search("b\\\\x3d(-?\d+);", tkk_expr).group(1)

        result = str(hours) + "." + str(int(a) + int(b))

    self.token_key = result
    return result


gtts.tts.gtts_token.Token._get_token_key = _get_token_key


# part of https://github.com/pndurette/gTTS/blob/master/gtts/tts.py#L165
def write_to_fp(self, fp):
    """Do the TTS API request and write bytes to a file-like object.

    Args:
        fp (file object): Any file-like object to write the ``mp3`` to.

    Raises:
        :class:`gTTSError`: When there's an error with the API request.
        TypeError: When ``fp`` is not a file-like object that takes bytes.

    """
    # When disabling ssl verify in requests (for proxies and firewalls),
    # urllib3 prints an insecure warning on stdout. We disable that.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    text_parts = self._tokenize(self.text)
    log.debug("text_parts: %i", len(text_parts))
    assert text_parts, 'No text to send to TTS API'

    for idx, part in enumerate(text_parts):
        try:
            # Calculate token
            part_tk = self.token.calculate_token(part)
        except requests.exceptions.RequestException as e:  # pragma: no cover
            log.debug(str(e), exc_info=True)
            raise gTTSError(
                "Connection error during token calculation: %s" %
                str(e))

        payload = {'ie': 'UTF-8',
                   'q': part,
                   'tl': self.lang,
                   'ttsspeed': self.speed,
                   'total': len(text_parts),
                   'idx': idx,
                   'client': 'tw-ob',
                   'textlen': _len(part),
                   'tk': part_tk}

        log.debug("payload-%i: %s", idx, payload)

        try:
            # Request
            r = requests.get(self.GOOGLE_TTS_URL,
                             params=payload,
                             headers=self.GOOGLE_TTS_HEADERS,
                             # proxies=urllib.request.getproxies(),
                             proxies=proxies('tts_google'),
                             verify=False)

            log.debug("headers-%i: %s", idx, r.request.headers)
            log.debug("url-%i: %s", idx, r.request.url)
            log.debug("status-%i: %s", idx, r.status_code)

            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            # Request successful, bad response
            raise gTTSError(tts=self, response=r)
        except requests.exceptions.RequestException as e:  # pragma: no cover
            # Request failed
            raise gTTSError(str(e))

        try:
            # Write
            # for chunk in r.iter_content(chunk_size=1024):
            for chunk in r.iter_content(chunk_size=self._buff_size):
                fp.write(chunk)
            log.debug("part-%i written to %s", idx, fp)
        except (AttributeError, TypeError) as e:
            raise TypeError(
                "'fp' is not a file-like object or it does not take bytes: %s" %
                str(e))


gtts.gTTS.write_to_fp = write_to_fp


class FPBranching:
    def __init__(self, fps):
        self._fps = fps if isinstance(fps, (list, tuple)) else [fps]

    def write(self, data):
        for fp in self._fps:
            fp.write(data)


class Google(gtts.gTTS):
    def __init__(self, text, buff_size, lang='en', slow=False, *_, **__):
        super().__init__(text, lang, slow, lang_check=False)
        self._buff_size = buff_size

    def stream_to_fps(self, fps):
        self.write_to_fp(FPBranching(fps))
