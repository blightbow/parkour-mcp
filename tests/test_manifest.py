"""Checks over the shipped ``manifest.json`` that no other suite covers.

``scripts/check_manifest_tools.py`` already gates the tool list against
registration. These are the declarations that gate *credentials*, where a
silent regression costs a user their API key rather than a stale doc.
"""

import json
from pathlib import Path

import pytest

MANIFEST = json.loads((Path(__file__).parent.parent / "manifest.json").read_text())

# Every user_config key that carries a secret.
CREDENTIAL_FIELDS = ("KAGI_API_KEY", "GITHUB_TOKEN", "S2_API_KEY")


class TestCredentialSensitivity:
    """Pins a deliberate deviation, documented in ``.claude/TECH_DEBT.md``.

    ``sensitive: true`` is the correct declaration for these fields and buys
    input masking plus encrypted storage. It is off on purpose: Claude Desktop
    on Windows forwards the stored ``__encrypted__:`` ciphertext into the
    server environment instead of decrypting it first
    (anthropics/claude-code#78296), so the flag is the difference between a key
    that works and one that cannot. Non-sensitive fields in the same
    ``user_config`` resolve correctly, which is what makes the flip a fix.

    **Delete this class when #78296 is fixed**, in the same change that
    restores ``"sensitive": true`` and removes the TECH_DEBT entry. It exists
    so that restore is a deliberate act rather than a silent drift back.
    """

    @pytest.mark.parametrize("field", CREDENTIAL_FIELDS)
    def test_a_credential_field_is_declared_non_sensitive(self, field):
        entry = MANIFEST["user_config"][field]
        assert entry["sensitive"] is False, (
            f"{field} is marked sensitive. On Windows that makes Claude Desktop "
            "forward ciphertext instead of the key (anthropics/claude-code#78296). "
            "If that bug is fixed, delete this class and the TECH_DEBT entry too."
        )

    @pytest.mark.parametrize("field", CREDENTIAL_FIELDS)
    def test_a_credential_field_reaches_the_server(self, field):
        """The env map only forwards keys declared in it.

        A user_config entry with no matching env line is collected by the
        settings UI and then dropped, which looks identical to the user as a
        key that does not work.
        """
        env = MANIFEST["server"]["mcp_config"]["env"]
        assert env.get(field) == "${user_config." + field + "}"

    def test_every_declared_env_key_has_a_user_config_entry(self):
        """The converse: a substitution with no field behind it expands to a
        template string that reaches the server as a literal ``${...}``."""
        env = MANIFEST["server"]["mcp_config"]["env"]
        for key, value in env.items():
            if value.startswith("${user_config."):
                assert key in MANIFEST["user_config"], (
                    f"{key} substitutes a user_config field that is not declared"
                )
