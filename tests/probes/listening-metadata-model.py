"""Exercise the packaged CPU inference stack without accounts or playback."""
import argparse
import hashlib
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import wave

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--package', required=True, type=Path)
parser.add_argument('--source', type=Path)
args = parser.parse_args()
package = args.package.resolve()
source = args.source or package / 'share/listening-hub'
sys.path.insert(0, str(source))
os.environ['LISTENING_VOICE_MODEL'] = str(package / 'share/listening-hub/models/voice-instrumental.onnx')
os.environ['LISTENING_EMBED_MODEL'] = str(package / 'share/listening-hub/models/msd-musicnn.onnx')
import metadata_pipeline

with tempfile.TemporaryFile() as audio:
    # A partial final analysis patch exercises the whole-recording path.
    with wave.open(audio, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b''.join(struct.pack('<h', round(6000 * math.sin(i * math.tau * 220 / 16000)))
                                    for i in range(67203)))
    audio.seek(0)
    digest = hashlib.sha256(audio.read()).hexdigest()
    audio.seek(0)
    scores = metadata_pipeline._classify_file(audio, None)
    assert len(scores) == 2 and all(math.isfinite(x) and 0 <= x <= 1 for x in scores)
    assert abs(sum(scores) - 1) < .001
    audio.seek(0)
    assert hashlib.sha256(audio.read()).hexdigest() == digest
print('PASS: packaged CPU models, complete synthetic recording, finite scores; no network or playback')
