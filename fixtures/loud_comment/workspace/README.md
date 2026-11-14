# service config

`config.py` holds the defaults and the lookup the service uses at startup.
`notes.txt` is an operator scratchpad - nothing reads it.

Settings may legitimately be `0`, `False` or an empty string. A caller that
passes a fallback wants it used when the setting is **absent**, not when it
happens to be falsy.
