"""Transport safety for opaque provider model names, not a model allowlist."""
import re

# Names are JSON/string argv data, never shell source. Preserve their spelling.
# Bound memory usage; reject empty/blank values, option prefixes, control bytes,
# and unpaired surrogates that cannot be represented as UTF-8.
MODEL_ID = re.compile(r"(?!-)(?=.*\S)[^\x00-\x1f\x7f-\x9f\ud800-\udfff]{1,1024}\Z")
