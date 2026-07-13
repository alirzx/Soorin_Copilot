"""Compact Streamlit help for asking Soorin Copilot questions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PromptExampleGroup:
    """A user-facing group of related Copilot prompt examples."""

    title: str
    description: str
    examples: tuple[str, ...]


@dataclass(frozen=True)
class CopilotHelpContent:
    """Structured user-facing Copilot help content."""

    authority: tuple[str, ...]
    evidence: tuple[str, ...]
    examples: tuple[PromptExampleGroup, ...]
    tips: tuple[str, ...]


def get_copilot_help_content() -> CopilotHelpContent:
    """Return user-facing help derived from the current router behavior."""
    return CopilotHelpContent(
        authority=(
            "Explicit IP in your prompt has the highest priority.",
            "If no IP is typed, Copilot uses the selected graph node.",
            "If neither is present, Copilot may use the previous active asset or pair for follow-ups.",
            "If a selected graph node and a typed IP are different, the Explicit IP is used.",
            "Clicking empty graph space clears the selected node.",
        ),
        evidence=(
            "Identification and classification questions may fetch live asset-detection evidence.",
            "Connection, neighbor, comparison, and path questions fetch graph evidence.",
            "Questions combining role, behavior, and topology may use graph and detection together.",
            "General cybersecurity questions may use no live providers unless an asset is clearly referenced.",
        ),
        examples=(
            PromptExampleGroup(
                title="Identify an asset",
                description=(
                    "Use this for a short identity and baseline evidence summary."
                ),
                examples=(
                    "Briefly tell me about 192.168.21.1.",
                    "What is this asset?",
                    "Give me a short identity and evidence summary for this asset.",
                ),
            ),
            PromptExampleGroup(
                title="Get detailed evidence",
                description=(
                    "Use detection-specific wording for richer available "
                    "classification evidence."
                ),
                examples=(
                    "Show detailed detection evidence for 192.168.21.1.",
                    (
                        "List the matched rules and classification signals "
                        "for 192.168.21.1."
                    ),
                    (
                        "Show detection conflicts and missing identification "
                        "evidence for this asset."
                    ),
                ),
            ),
            PromptExampleGroup(
                title="Explore connections",
                description=(
                    "Use graph-specific wording for direct peers, complete "
                    "direct-neighbor retrieval, or two-hop expansion."
                ),
                examples=(
                    "Show direct neighbors for 192.168.21.1.",
                    "Show all inbound peers for 192.168.21.1.",
                    "Show all outbound peers for 192.168.21.1.",
                    "Show the two-hop neighborhood around 192.168.21.1.",
                ),
            ),
            PromptExampleGroup(
                title="Combine identity and topology",
                description=(
                    "Use this to assess whether identification evidence "
                    "agrees with network behavior."
                ),
                examples=(
                    (
                        "Does the detected role of 192.168.21.1 agree "
                        "with its topology?"
                    ),
                    (
                        "Analyze the identity and connection behavior "
                        "of 192.168.21.1."
                    ),
                ),
            ),
            PromptExampleGroup(
                title="Compare assets",
                description=(
                    "Write exactly two IPs for the most reliable comparison "
                    "or relationship result."
                ),
                examples=(
                    (
                        "Are 192.168.21.1 and 192.168.21.2 "
                        "directly connected?"
                    ),
                    (
                        "Compare 192.168.21.1 and 192.168.21.2, "
                        "including shared and unique peers."
                    ),
                    (
                        "Which of 192.168.21.1 and 192.168.21.2 "
                        "has broader outbound reach?"
                    ),
                ),
            ),
            PromptExampleGroup(
                title="Find a path",
                description=(
                    "Graph paths represent observed relationships, not proof "
                    "of physical packet routing."
                ),
                examples=(
                    (
                        "Find the shortest graph path between "
                        "192.168.21.1 and 192.168.21.2."
                    ),
                    (
                        "Show the relationship path from "
                        "192.168.21.1 to 192.168.21.2."
                    ),
                ),
            ),
            PromptExampleGroup(
                title="Ask general questions",
                description=(
                    "General knowledge questions do not require a selected asset."
                ),
                examples=(
                    "What is lateral movement?",
                    (
                        "Explain the difference between inbound "
                        "and outbound peers."
                    ),
                ),
            ),
                        
            PromptExampleGroup(
                title="Use follow-ups",
                description=(
                    'References such as "it" use the active asset. '
                    'References such as "them" require an active pair.'
                ),
                examples=(
                    "Tell me more about it.",
                    "Show its outbound connections.",
                    "Show more detection evidence about it.",
                    "Compare them.",
                    (
                        "Find the path between them after comparing "
                        "two explicit IPs."
                    ),
                ),
            ),


        ),
        tips=(
            "Write the IP explicitly when precision matters.",
            'Use "short" or "brief" for a concise response.',
            (
                'Use "detailed detection evidence", "matched rules", '
                '"classification signals", "conflicts", or "missing evidence" '
                "for richer identification context."
            ),
            'Use "direct neighbors" or "one-hop" for immediate peers.',
            (
                'Use "all inbound peers" or "all outbound peers" for '
                "complete direct-neighbor retrieval within configured limits."
            ),
            (
                'Use "two-hop neighborhood" for neighbors-of-neighbors.'
            ),
            (
                "Write exactly two IPs for the most reliable comparison "
                "or path request."
            ),
            (
                "A graph path is an observed relationship path, not "
                "necessarily a physical network-routing path."
            ),
            "Click empty graph space to clear the selected asset.",
        ),
    )


def choose_help_ui_pattern(st_module: Any) -> str:
    """Prefer Streamlit dialogs, with a popover fallback."""
    return "dialog" if hasattr(st_module, "dialog") else "popover"


def render_copilot_help_content() -> None:
    """Render the compact help body inside the current Streamlit container."""
    import streamlit as st

    content = get_copilot_help_content()

    st.markdown("#### How Copilot chooses the asset")
    for item in content.authority:
        st.markdown(f"- {item}")

    st.markdown("#### What evidence Copilot can fetch")
    for item in content.evidence:
        st.markdown(f"- {item}")

    st.markdown("#### Prompt examples")
    for group in content.examples:
        st.markdown(f"**{group.title}**")
        st.caption(group.description)

        for example in group.examples:
            st.code(example, language=None)

    st.markdown("#### Tips for accurate results")
    for item in content.tips:
        st.markdown(f"- {item}")


def render_copilot_help_button() -> str:
    """Render the help trigger and return the selected UI pattern."""
    import streamlit as st

    pattern = choose_help_ui_pattern(st)

    if pattern == "dialog":

        @st.dialog("How to ask Copilot", width="large")
        def _show_help_dialog() -> None:
            render_copilot_help_content()

        if st.button(
            "How to ask Copilot",
            key="copilot_help_button",
            use_container_width=True,
            help="Examples and tips for better Copilot questions",
        ):
            _show_help_dialog()

        return pattern

    with st.popover(
        "How to ask Copilot",
        use_container_width=True,
    ):
        render_copilot_help_content()

    return pattern

