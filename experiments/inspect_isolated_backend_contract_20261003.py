"""Read only isolation-related names and schema, never credential values."""

from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anythingllm_state import read_env_values

storage = Path.home() / 'AppData/Roaming/anythingllm-desktop/storage'
values = read_env_values(storage / '.env')
print('EMBED ENV KEYS', [key for key in values if any(
    token in key for token in ('EMBED', 'OPENROUTER', 'VECTOR', 'TEXT_SPLITTER'))])
with sqlite3.connect((storage / 'anythingllm.db').as_uri() + '?mode=ro', uri=True) as db:
    print('SCHEMA', db.execute(
        "select sql from sqlite_master where name in ('system_settings','api_keys')").fetchall())
    print('SETTING LABELS', db.execute(
        "select label from system_settings where label like '%embed%' or label like '%splitter%'").fetchall())
