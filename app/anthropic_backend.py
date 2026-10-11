"""Real drafting backend, via the Anthropic API.

Off by default. Enable with LLM_MODE=anthropic and an ANTHROPIC_API_KEY.

What the model gets is exactly the same `DraftContext` the mock backend gets:
finished strings, no raw numbers, no way to compute anything. What it returns
goes through the same guardrail. Switching backends changes the prose and
nothing else about the guarantees.

The prompt says so explicitly too — belt and braces — but the guarantee comes
from the guardrail, not from the instruction being obeyed.
"""

from __future__ import annotations

import json
from dataclasses import fields

from .config import ANTHROPIC_EFFORT, ANTHROPIC_MAX_TOKENS, ANTHROPIC_MODEL
from .drafting import DraftContext

SYSTEM_PROMPT = """Tu rédiges des relances de factures impayées pour un garage \
automobile indépendant français. Tu écris en français, pour des clients de \
proximité que le garagiste connaît personnellement.

RÈGLES ABSOLUES

1. Tu n'écris AUCUN chiffre qui ne figure pas mot pour mot dans les données \
fournies. Aucun total, aucun calcul, aucune date déduite, aucun pourcentage, \
aucun numéro de compte. Si un chiffre n'est pas dans les données, il n'a pas sa \
place dans le message. Recopie les montants exactement tels qu'ils te sont \
donnés, y compris le format (« 2 153,04 € »).
2. Tu ne promets rien au nom du garage : pas de remise, pas d'échéancier, pas \
de geste commercial, pas de délai supplémentaire autre que celui fourni.
3. Tu ne menaces jamais. Même au niveau 4, le registre est juridique et factuel, \
sans agressivité ni jugement sur le client.
4. Tu n'inventes aucun fait : pas de relance antérieure qui n'est pas \
mentionnée, pas de conversation téléphonique, pas d'engagement du client.

TON PAR NIVEAU

Niveau 1 — courtois. On part du principe d'un simple oubli. Aucune mention de \
pénalité, aucune pression. Le client doit pouvoir répondre sans se sentir mis \
en cause.
Niveau 2 — ferme mais toujours commercial. On rappelle les conditions de \
règlement et le retard constaté. On propose d'appeler en cas de difficulté.
Niveau 3 — sérieux. Dernier avis avant mise en demeure. On annonce clairement \
la suite sans dramatiser.
Niveau 4 — registre juridique formel. Courrier, pas e-mail. Factuel, sobre, \
aucune émotion. La mention « LETTRE RECOMMANDÉE AVEC ACCUSÉ DE RÉCEPTION » doit \
figurer en tête du courrier.

FORME

Réponds uniquement par un objet JSON à deux clés : « subject » (l'objet du \
message, une ligne) et « body » (le corps complet, prêt à envoyer, signé par le \
gérant). Le corps est du texte brut, sans Markdown."""

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["subject", "body"],
    "additionalProperties": False,
}


def _render_context(context: DraftContext) -> str:
    """The context as a flat list of labelled strings — nothing to compute."""
    lines = ["DONNÉES DE LA RELANCE (chaînes déjà formatées, à recopier telles quelles)", ""]
    for field in fields(context):
        value = getattr(context, field.name)
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            value = "oui" if value else "non"
        lines.append(f"{field.name} : {value}")
    return "\n".join(lines)


def build_prompt(context: DraftContext) -> str:
    """Exposed so the prompt can be inspected without spending a call."""
    instructions = (
        f"Rédige la relance de niveau {context.level} ({context.level_name}) "
        f"à envoyer par {context.channel}."
    )
    if context.level == 4:
        instructions += (
            " Il s'agit d'une mise en demeure : commence le corps par la mention "
            "de la lettre recommandée avec accusé de réception, puis les "
            "coordonnées, la date et l'objet."
        )
    if not context.penalty:
        instructions += (
            " Aucune pénalité n'est réclamée sur cette facture : n'en mentionne "
            "aucune, sous aucune forme."
        )
    return f"{_render_context(context)}\n\n{instructions}"


def draft(context: DraftContext) -> tuple[str, str]:
    """Call the API and return (subject, body).

    Raises if the SDK is missing or the call fails — a failed draft must be a
    visible error, never a silently empty message in the owner's queue.
    """
    try:
        import anthropic
    except ImportError as error:  # pragma: no cover
        raise RuntimeError(
            "LLM_MODE=anthropic mais le paquet 'anthropic' n'est pas installé. "
            "Lancez : pip install -r requirements.txt"
        ) from error

    # Credentials may come from ANTHROPIC_API_KEY or from an `ant auth login`
    # profile. The SDK resolves them lazily, at request time, so both the
    # client and the call sit inside the same guard.
    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=ANTHROPIC_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            output_config={
                "effort": ANTHROPIC_EFFORT,
                "format": {"type": "json_schema", "schema": _RESPONSE_SCHEMA},
            },
            messages=[{"role": "user", "content": build_prompt(context)}],
        )
    except anthropic.AuthenticationError as error:
        raise RuntimeError(
            "Clé d’API refusée. Vérifiez ANTHROPIC_API_KEY, ou repassez en "
            "mode gratuit avec LLM_MODE=mock."
        ) from error
    except (TypeError, ValueError) as error:
        if "authentication" not in str(error).lower():
            raise
        raise RuntimeError(
            "LLM_MODE=anthropic mais aucune clé d’API n’est configurée. "
            "Renseignez ANTHROPIC_API_KEY, ou repassez en mode gratuit avec "
            "LLM_MODE=mock."
        ) from error

    if response.stop_reason == "refusal":
        raise RuntimeError(
            "Le modèle a refusé de rédiger ce message "
            f"({getattr(response.stop_details, 'category', 'motif inconnu')})."
        )

    text = next((block.text for block in response.content if block.type == "text"), "")
    if not text:
        raise RuntimeError("Réponse vide du modèle.")

    payload = json.loads(text)
    return payload["subject"], payload["body"]
