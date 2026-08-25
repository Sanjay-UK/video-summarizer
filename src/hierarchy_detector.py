"""Detect and structure hierarchical content from markdown summaries."""

import re
from typing import List, Dict, Tuple


def detect_hierarchy_level(line: str) -> int:
    """Detect hierarchy level from markdown indentation/structure.

    Returns:
        0 (main point), 1 (subpoint), 2+ (sub-subpoint, etc.)
    """
    if not line.strip():
        return 0

    # Count leading spaces before bullet marker
    leading_spaces = len(line) - len(line.lstrip())

    # Check for markdown heading markers (# ## ###)
    heading_match = re.match(r'^(#+)\s+', line)
    if heading_match:
        num_hashes = len(heading_match.group(1))
        return num_hashes - 1  # # = 0, ## = 1, ### = 2

    # Estimate hierarchy from indentation (every 2 spaces = 1 level)
    # 0 spaces = 0, 2-3 spaces = 1, 4-5 spaces = 2, etc.
    return max(0, leading_spaces // 2)


def detect_transition_need(text: str) -> Tuple[bool, str]:
    """Check if text needs transition phrase and which one.

    Returns:
        (needs_injection, transition_phrase)
        Example: (True, "For example,") or (False, "")
    """
    text_lower = text.lower()

    # Already has a transition phrase
    transition_phrases = [
        "for example", "for instance", "such as", "like",
        "to illustrate", "consider", "this manifests",
        "what this looks like", "here's", "the reason",
        "this means", "this demonstrates", "meanwhile",
        "additionally", "also", "there's", "furthermore",
        "moreover", "in addition", "likewise", "similarly",
    ]

    for phrase in transition_phrases:
        if phrase in text_lower:
            return False, ""

    # Text starts with a descriptor followed by colon (good candidate for transition)
    # Examples: "Education Costs:", "Job Market Challenges:"
    if re.match(r'^[A-Z][A-Za-z\s]+:', text):
        return True, "For example,"

    return False, ""


def extract_hierarchy_structure(summary_text: str) -> List[Dict]:
    """Parse summary into structured hierarchy.

    Returns:
        List of dicts with:
        - text: the point text
        - level: hierarchy level (0, 1, 2+)
        - needs_transition: bool, whether to inject transition phrase
        - transition_phrase: string to inject if needed
    """
    lines = summary_text.split('\n')
    structure = []

    for line in lines:
        stripped = line.strip()

        # Skip empty lines
        if not stripped:
            continue

        # Detect hierarchy level
        level = detect_hierarchy_level(line)

        # Clean the text (remove markdown markers)
        clean_text = re.sub(r'^#+\s+', '', stripped)  # Remove ## markers
        clean_text = re.sub(r'^[-*+•]\s+', '', clean_text)  # Remove bullet markers

        # Detect if transition phrase is needed
        needs_transition, phrase = detect_transition_need(clean_text)

        structure.append({
            'text': clean_text,
            'level': level,
            'needs_transition': needs_transition,
            'transition_phrase': phrase,
        })

    return structure


def get_pause_timing(level: int, is_transition: bool = False) -> int:
    """Get pause timing in milliseconds based on hierarchy level.

    Args:
        level: Hierarchy level (0, 1, 2+)
        is_transition: Whether this is a transition between levels

    Returns:
        Pause time in milliseconds
    """
    if is_transition:
        # Pause before introducing new content
        if level == 0:
            return 1000  # Main points get longer pause
        elif level == 1:
            return 800   # Sub-points get medium pause
        else:
            return 300   # Details get short pause

    # Shorter pause within same level
    return 300


def get_speech_rate(level: int) -> int:
    """Get speech rate percentage based on hierarchy level.

    Args:
        level: Hierarchy level (0, 1, 2+)

    Returns:
        Speech rate as percentage (85-110)
    """
    if level == 0:
        return 85  # Main points: slow, clear
    elif level == 1:
        return 100  # Sub-points: normal
    else:
        return 110  # Details: slightly faster


if __name__ == "__main__":
    # Test
    sample = """Key Problem: India has 87 million young people

Impact on Youth: Gen Z faces barriers:
  Education Costs: Many paid for education
  Job Market Challenges:
    - Government jobs are limited
    - Private sector hiring decreased
"""

    hierarchy = extract_hierarchy_structure(sample)
    for i, item in enumerate(hierarchy):
        print(f"{i+1}. Level {item['level']}: {item['text']}")
        if item['needs_transition']:
            print(f"   → Add transition: '{item['transition_phrase']}'")
