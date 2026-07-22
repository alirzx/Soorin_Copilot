"""Compact Streamlit help for asking Soorin Cyber Copilot questions."""

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
    """Return help content for the current Copilot capabilities."""
    return CopilotHelpContent(
        authority=(
            "An IP written in your prompt has the highest priority.",
            "Two explicit IPs define the asset pair for comparison, relationship, or path questions.",
            "If no IP is written, Copilot can use the selected topology asset.",
            "Follow-up questions can use the previous active asset or asset pair.",
            "A new general topic is handled separately from the previous asset investigation.",
            "A typed IP overrides a different selected topology asset.",
        ),
        evidence=(
            "Asset Profile provides identity, hostname, operating system, role, services, and inventory evidence.",
            "Detection provides classification, confidence, behavior, anomaly, and risk evidence.",
            "Graph provides summaries, neighbors, direct relationships, comparisons, and observed paths.",
            "Knowledge retrieval supports general cybersecurity concepts and investigation guidance.",
            "Combined investigations can use Profile, Detection, Graph, and Knowledge evidence together.",
            "Copilot selects evidence according to the question instead of fetching every source for every request.",
        ),
        examples=(
            PromptExampleGroup(
                title="Summarize an asset",
                description="Ask for a short or complete overview of one asset.",
                examples=(
                    "Briefly summarize 192.168.0.149.",
                    "Tell me about 192.168.0.125.",
                    "Give me a short evidence summary for 192.168.0.149.",
                    "Analyze 192.168.0.149 using all available asset evidence.",
                ),
            ),
            PromptExampleGroup(
                title="Check identity and role",
                description="Use these prompts for Profile and classification evidence.",
                examples=(
                    "What is 192.168.0.149 and what role does it serve?",
                    "Show the identity and profile evidence for 192.168.0.125.",
                    "What operating system and services are observed on 192.168.0.149?",
                    "Does the detected role of 192.168.0.149 match its profile?",
                ),
            ),
            PromptExampleGroup(
                title="Review detections and risk",
                description="Ask about classifications, anomalies, behavior, or security risk.",
                examples=(
                    "Show the detection evidence for 192.168.0.149.",
                    "What anomalies are associated with 192.168.0.149?",
                    "Explain the risk score and classification confidence for 192.168.0.149.",
                    "List the main detection signals and conflicts for 192.168.0.149.",
                ),
            ),
            PromptExampleGroup(
                title="Explore network connections",
                description="Ask for direct peers, traffic direction, or wider graph scope.",
                examples=(
                    "Show the direct neighbors of 192.168.0.149.",
                    "Show all inbound peers for 192.168.0.125.",
                    "Show all outbound peers for 192.168.0.149.",
                    "Show the two-hop neighborhood around 192.168.0.149.",
                ),
            ),
            PromptExampleGroup(
                title="Compare two assets",
                description="Write exactly two IPs and state what should be compared.",
                examples=(
                    "Compare 192.168.0.149 and 192.168.0.125.",
                    (
                        "Compare 192.168.0.149 and 192.168.0.125. "
                        "State their direct relationship first, then give three differences."
                    ),
                    (
                        "Which of 192.168.0.149 and 192.168.0.125 "
                        "has broader outbound reach?"
                    ),
                    (
                        "Compare the roles, detections, and network behavior of "
                        "192.168.0.149 and 192.168.0.125."
                    ),
                ),
            ),
            PromptExampleGroup(
                title="Check a direct relationship",
                description="Ask whether one asset directly communicates with another.",
                examples=(
                    (
                        "What is the direct relationship between "
                        "192.168.0.149 and 192.168.0.125?"
                    ),
                    (
                        "Does 192.168.0.149 communicate directly with "
                        "192.168.0.125?"
                    ),
                    (
                        "State the observed direction between "
                        "192.168.0.149 and 192.168.0.125."
                    ),
                    (
                        "Is the relationship between 192.168.0.149 and "
                        "192.168.0.125 one-way or bidirectional?"
                    ),
                ),
            ),
            PromptExampleGroup(
                title="Assess risk between assets",
                description="Combine relationship, Profile, and Detection evidence.",
                examples=(
                    (
                        "Does 192.168.0.149 create a security risk for "
                        "192.168.0.125?"
                    ),
                    (
                        "Assess the security risk from 192.168.0.149 to "
                        "192.168.0.125. State the direct relationship first."
                    ),
                    (
                        "Is there evidence of suspicious activity from "
                        "192.168.0.149 toward 192.168.0.125?"
                    ),
                    (
                        "Compare the detections and relationship of "
                        "192.168.0.149 and 192.168.0.125."
                    ),
                ),
            ),
            PromptExampleGroup(
                title="Find an observed path",
                description="Ask for an ordered relationship path between two assets.",
                examples=(
                    (
                        "Find the shortest graph path from "
                        "192.168.0.149 to 192.168.0.125."
                    ),
                    (
                        "Show the observed relationship path between "
                        "192.168.0.149 and 192.168.0.125."
                    ),
                    (
                        "How many graph hops separate "
                        "192.168.0.149 and 192.168.0.125?"
                    ),
                    (
                        "List the assets on the path from "
                        "192.168.0.149 to 192.168.0.125."
                    ),
                ),
            ),
            PromptExampleGroup(
                title="Run a combined investigation",
                description="Ask Copilot to combine multiple evidence sources.",
                examples=(
                    "Analyze 192.168.0.149 using Profile, Detection, and Graph evidence.",
                    (
                        "Does the detected role of 192.168.0.149 agree "
                        "with its network behavior?"
                    ),
                    (
                        "Investigate the identity, risk, and connections "
                        "of 192.168.0.149."
                    ),
                    (
                        "Explain the most important security concerns for "
                        "192.168.0.149 and recommend next checks."
                    ),
                ),
            ),
            PromptExampleGroup(
                title="Ask cybersecurity questions",
                description="General questions do not require a selected asset.",
                examples=(
                    "What is Kerberos? Answer in one sentence.",
                    "Explain lateral movement in simple terms.",
                    "What is the difference between LDAP and Kerberos?",
                    "What does password spraying mean in a SOC investigation?",
                ),
            ),
            PromptExampleGroup(
                title="Use follow-up questions",
                description="Use short references after establishing an active asset or pair.",
                examples=(
                    "Tell me more about it.",
                    "Show its detection evidence.",
                    "Show its outbound neighbors.",
                    "Compare them again, focusing on risk.",
                    "Find the path between them.",
                ),
            ),
        ),
        tips=(
            "Write the IP explicitly when accuracy matters.",
            "Write exactly two IPs for comparison, relationship, risk, or path questions.",
            'Use "brief" or "short" when you want a concise answer.',
            'Use "detailed" or "all available evidence" for a broader investigation.',
            'Use "inbound", "outbound", or "both directions" for connection questions.',
            'Ask Copilot to "state the direct relationship first" when comparing two assets.',
            "Use detection wording for classifications, anomalies, risk, and behavior.",
            "Use graph wording for neighbors, relationships, comparisons, and paths.",
            "Use follow-up words such as “it” after one asset and “them” after an asset pair.",
            "Click empty graph space to clear the selected topology asset.",
        ),
    )


def choose_help_ui_pattern(st_module: Any) -> str:
    """Prefer Streamlit dialogs, with a popover fallback."""
    return "dialog" if hasattr(st_module, "dialog") else "popover"


def render_copilot_help_content() -> None:
    """Render the help body inside the current Streamlit container."""
    import streamlit as st

    content = get_copilot_help_content()

    st.markdown("#### How Copilot selects assets")
    for item in content.authority:
        st.markdown(f"- {item}")

    st.markdown("#### Evidence Copilot can use")
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
            width="stretch",
            help="Examples and tips for better Copilot questions",
        ):
            _show_help_dialog()

        return pattern

    with st.popover(
        "How to ask Copilot",
        width="stretch",
    ):
        render_copilot_help_content()

    return pattern