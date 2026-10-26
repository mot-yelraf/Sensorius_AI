"""Verify inert configuration remains data even with HTML/JavaScript delimiters.

The browser gate separately parses scripts, rejects comments, and paints real
chart/gauge canvases. This test covers the serialization boundary itself.
"""
import json
from html.parser import HTMLParser
from sensorius.saiHtml import _json_script


def test_script_configuration_cannot_terminate_its_element():
    payload = {'label': '</script><script>alert("bad")</script>',
               'url': 'https://example.invalid/a?x=1&y=2',
               'text': '// data, not executable\n/* data */\u2028\u2029'}
    markup = _json_script('dashboard-config', payload)

    class Scripts(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = []
            self.content = ''

        def handle_starttag(self, tag, attrs):
            self.tags.append((tag, dict(attrs)))

        def handle_data(self, text):
            self.content += text

    parsed = Scripts()
    parsed.feed(markup)
    assert parsed.tags == [('script', {'type': 'application/json', 'id': 'dashboard-config'})]
    assert json.loads(parsed.content) == payload
    assert '<' not in parsed.content
    assert '&' not in parsed.content


def test_nonfinite_readings_remain_missing_values_in_valid_json():
    payload = {'sensor': [float('nan'), float('inf'), -float('inf'), 23.4]}
    markup = _json_script('dashboard-config', payload)
    encoded = markup.split('>', 1)[1].rsplit('</script>', 1)[0]
    assert json.loads(encoded) == {'sensor': [None, None, None, 23.4]}
