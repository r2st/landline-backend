"""Deal Progression Agent — per-agent LLM model + API key.

Chains off ``083_social_listening_agent``, the current head (this landed after
the referral and social-listening agents in the merge train, hence 084 rather
than the 082 it was first authored as).

Adds two columns to ``deal_progression_agents`` so each org can point its agent
at a specific OpenRouter model and (optionally) supply its own API key:

* ``llm_model`` — the OpenRouter model id the agent's AI touches run on. NOT
  NULL, defaults to the free-tier Llama.
* ``llm_api_key`` — the org's OpenRouter key, stored as Fernet ciphertext.
  Nullable: NULL means fall back to the org-level OPENROUTER_API_KEY in
  system_settings, then the global env key.
"""

from alembic import op
import sqlalchemy as sa

revision = "084_deal_agent_llm_config"
down_revision = "083_social_listening_agent"
branch_labels = None
depends_on = None

DEFAULT_LLM_MODEL = "meta-llama/llama-3.3-70b-instruct:free"


def upgrade() -> None:
    op.add_column(
        "deal_progression_agents",
        sa.Column(
            "llm_model", sa.String(128), nullable=False,
            server_default=DEFAULT_LLM_MODEL,
        ),
    )
    op.add_column(
        "deal_progression_agents",
        sa.Column("llm_api_key", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deal_progression_agents", "llm_api_key")
    op.drop_column("deal_progression_agents", "llm_model")
