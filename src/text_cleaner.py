"""Clean and format text for display and audio."""

import re
from src.domain_handler import DomainHandler
from src.hierarchy_detector import (
    extract_hierarchy_structure,
    get_pause_timing,
    get_speech_rate,
)


def strip_markdown(text: str) -> str:
    """Remove markdown formatting characters from text.

    Removes: *, _, #, **, ~~, etc.
    """
    # Remove bold/italic markers
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # **bold** → bold
    text = re.sub(r'\*(.+?)\*', r'\1', text)      # *italic* → italic
    text = re.sub(r'__(.+?)__', r'\1', text)      # __bold__ → bold
    text = re.sub(r'_(.+?)_', r'\1', text)        # _italic_ → italic
    text = re.sub(r'~~(.+?)~~', r'\1', text)      # ~~strike~~ → strike

    # Remove headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)

    # Remove links but keep text
    text = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', text)  # [text](url) → text

    # Remove code blocks
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    text = re.sub(r'`(.+?)`', r'\1', text)  # `code` → code

    return text


def convert_lists_to_prose(text: str) -> str:
    """Convert bullet lists to natural prose sentences.

    Instead of "Item 1: content", creates natural language:
    "This contains three points. First, content. Second, content..."
    """
    lines = text.split('\n')
    items = []
    other_lines = []

    for line in lines:
        # Check if this is a bullet point
        if re.match(r'^\s*[-*+•]\s+', line):
            content = re.sub(r'^\s*[-*+•]\s+', '', line).strip()
            content = content.rstrip('.')  # Remove trailing period
            items.append(content)
        else:
            other_lines.append(line)

    # If we found bullet items, convert to prose
    if items:
        ordinals = ['First', 'Second', 'Third', 'Fourth', 'Fifth', 'Sixth', 'Seventh', 'Eighth', 'Ninth', 'Tenth']
        prose_items = []

        for i, item in enumerate(items):
            ordinal = ordinals[i] if i < len(ordinals) else f"Point {i + 1}"
            prose_items.append(f"{ordinal}, {item}.")

        # Combine other content with prose items
        result = []
        if other_lines:
            result.extend([line for line in other_lines if line.strip()])
        result.extend(prose_items)
        return '\n\n'.join(result)

    return text


def detect_hierarchy_level(line: str) -> int:
    """Detect indentation/hierarchy level of a line.

    Returns 0 for main level, 1 for sub-level, 2 for sub-sub-level, etc.
    """
    if not line.strip():
        return 0

    # Count leading spaces
    leading_spaces = len(line) - len(line.lstrip())

    # Each 2-4 spaces = 1 hierarchy level
    return leading_spaces // 2


def create_hierarchical_transcript(text: str) -> str:
    """Convert markdown with hierarchy to audio-friendly hierarchical text.

    Shows clear differentiation between main points, subpoints, and sub-subpoints.
    """
    lines = text.split('\n')
    result = []
    current_section = None
    current_section_level = -1
    subsection_items = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check if this is a heading
        heading_match = re.match(r'^(#+)\s+(.+)$', stripped)

        if heading_match:
            # Save previous section if exists
            if current_section and subsection_items:
                result.append(f'{current_section}. With the following details: {" ".join(subsection_items)}.')
                subsection_items = []
            elif current_section:
                result.append(f'{current_section}.')

            # Start new section
            heading_level = len(heading_match.group(1))
            section_text = heading_match.group(2)
            current_section = section_text
            current_section_level = heading_level

        elif current_section:
            # This is content under the current section
            # Remove bullet markers
            content = re.sub(r'^\s*[-*+•]\s+', '', stripped)

            # If this is a bullet point, add as subpoint
            if re.match(r'^\s*[-*+•]', stripped):
                subsection_items.append(f'"{content}"')
            else:
                # Regular text - add to result if it doesn't look like metadata
                if not any(x in content.lower() for x in ['created at', 'completed at', 'status']):
                    if current_section and not subsection_items:
                        current_section = f'{current_section}. {content}'
                    elif subsection_items:
                        subsection_items.append(f'Also, {content}')

    # Add final section
    if current_section and subsection_items:
        result.append(f'{current_section}. With the following details: {" ".join(subsection_items)}.')
    elif current_section:
        result.append(f'{current_section}.')

    return ' '.join(result)


def create_audio_friendly_transcript_with_hierarchy(text: str) -> tuple:
    """Create audio-friendly transcript with hierarchy cues and pauses.

    Uses pause timing and rate markers for SSML conversion.

    Args:
        text: Original markdown-formatted summary

    Returns:
        Tuple of (audio_transcript, hierarchy_data)
        - audio_transcript: Text with [PAUSE_Xms] and [RATE_X%] markers
        - hierarchy_data: List of structured points with hierarchy levels
    """
    # Step 1: Extract hierarchy structure
    hierarchy = extract_hierarchy_structure(text)

    # Step 2: Build audio transcript with markers
    transcript_parts = []
    prev_level = -1

    for item in hierarchy:
        point_text = item['text']
        level = item['level']

        # Apply domain-aware symbol handling
        point_text = DomainHandler.process_for_audio(point_text)

        # Inject transition phrase if needed
        if item['needs_transition'] and item['transition_phrase']:
            point_text = f"{item['transition_phrase']} {point_text}"

        # Add pause before this point if level changed
        if prev_level >= 0 and prev_level != level:
            pause_ms = get_pause_timing(level, is_transition=True)
            transcript_parts.append(f"[PAUSE_{pause_ms}ms]")

        # Add rate change marker for this level
        rate = get_speech_rate(level)
        transcript_parts.append(f"[RATE_{rate}%]")

        # Add the actual point text
        transcript_parts.append(point_text)

        prev_level = level

    # Join all parts with spaces (markers will be processed later)
    audio_transcript = " ".join(transcript_parts)

    # Clean up whitespace around markers
    audio_transcript = re.sub(r'\s+(\[PAUSE|\[RATE)', r'\1', audio_transcript)  # Remove space before markers
    audio_transcript = re.sub(r'(\])\s+', r'\1 ', audio_transcript)  # Space after markers

    # Protect markers before stripping markdown (strip_markdown removes underscores)
    # Use unique placeholders that won't be affected by strip_markdown
    marker_map = {}

    def replace_marker(match):
        marker = match.group(0)
        # Use a placeholder with numbers that won't be affected
        placeholder = f"@MARKER{len(marker_map)}@"
        marker_map[placeholder] = marker
        return placeholder

    # Replace all markers with placeholders
    audio_transcript = re.sub(r'\[(?:PAUSE|RATE)_\d+(?:ms|%)\]', replace_marker, audio_transcript)

    # Strip markdown from the enhanced text for cleanliness
    audio_transcript = strip_markdown(audio_transcript)

    # Restore markers
    for placeholder, marker in marker_map.items():
        audio_transcript = audio_transcript.replace(placeholder, marker)

    return audio_transcript, hierarchy


def create_audio_friendly_transcript(text: str) -> str:
    """Create audio-friendly transcript with domain-aware symbol handling.

    Pipeline:
    1. Strip markdown formatting (bold, italic, headers, etc.)
    2. Convert bullet lists to natural prose
    3. Detect content domain (code/math/chemistry/physics)
    4. Apply domain-specific symbol rules
    5. Clean whitespace and expand abbreviations

    Args:
        text: Markdown-formatted summary text

    Returns:
        Plain text ready for TTS with proper pronunciations and hierarchy
    """
    # Step 1: Strip markdown formatting
    audio_text = strip_markdown(text)

    # Step 2: Convert lists to natural prose
    audio_text = convert_lists_to_prose(audio_text)

    # Step 3: Apply domain-aware symbol handling
    audio_text = DomainHandler.process_for_audio(audio_text)

    # Step 4: Expand abbreviations for natural speech
    abbreviations = {
        r'\be\.g\.\b': 'for example',
        r'\bi\.e\.\b': 'that is',
        r'\betc\.\b': 'and so on',
        r'\bvs\.\b': 'versus',
        r'\be\.g\b': 'for example',
        r'\bi\.e\b': 'that is',
        r'\bdr\.': 'doctor',
        r'\bmr\.': 'mister',
        r'\bmrs\.': 'missus',
        r'\bms\.': 'miss',
        r'\bprof\.': 'professor',
        r'\bincl\.': 'including',
        r'\besq\.': 'esquire',
    }

    for pattern, replacement in abbreviations.items():
        audio_text = re.sub(pattern, replacement, audio_text, flags=re.IGNORECASE)

    # Step 5: Clean whitespace
    audio_text = re.sub(r' +', ' ', audio_text)
    audio_text = re.sub(r'\n\n+', ' ', audio_text)  # Collapse multiple newlines to space
    audio_text = re.sub(r'\n+', ' ', audio_text)     # Remove single newlines
    audio_text = audio_text.strip()

    return audio_text


def create_audio_transcript(text: str) -> str:
    """Create a clean audio-friendly transcript from markdown summary.

    This is specifically designed for TTS - removes ALL markdown and
    special characters that would be read literally by Edge TTS.
    """
    # CRITICAL: Start with the original markdown text
    audio_text = text

    # Step 1: Remove all markdown formatting
    # Remove bold (** or __) - keep content
    audio_text = re.sub(r'\*\*(.+?)\*\*', r'\1', audio_text)
    audio_text = re.sub(r'__(.+?)__', r'\1', audio_text)

    # Remove italic (* or _) - keep content
    audio_text = re.sub(r'\*(.+?)\*', r'\1', audio_text)
    audio_text = re.sub(r'_(.+?)_', r'\1', audio_text)

    # Remove strikethrough
    audio_text = re.sub(r'~~(.+?)~~', r'\1', audio_text)

    # Remove headers (# ## ### etc) but keep content
    audio_text = re.sub(r'^#{1,6}\s+', '', audio_text, flags=re.MULTILINE)

    # Step 2: Remove list markers
    # Remove bullet points (-, *, +)
    audio_text = re.sub(r'^\s*[-*+]\s+', '', audio_text, flags=re.MULTILINE)
    # Remove numbered lists
    audio_text = re.sub(r'^\s*\d+\.\s+', '', audio_text, flags=re.MULTILINE)

    # Step 3: Remove markdown links but keep the text [text](url) → text
    audio_text = re.sub(r'\[(.+?)\]\([^)]+\)', r'\1', audio_text)

    # Step 4: Remove code formatting
    # Remove inline code `code` → code (BUT keep the content!)
    audio_text = re.sub(r'`([^`]+)`', r'\1', audio_text)
    # Remove code blocks
    audio_text = re.sub(r'```[^`]*```', '', audio_text, flags=re.DOTALL)

    # Step 5: Remove parenthetical asides (keep the content inside)
    audio_text = re.sub(r'\(([^)]+)\)', r'\1', audio_text)
    audio_text = re.sub(r'\[([^\]]+)\]', r'\1', audio_text)

    # Step 6: Expand abbreviations for natural speech
    abbreviations = {
        r'\be\.g\.\b': 'for example',
        r'\bi\.e\.\b': 'that is',
        r'\betc\.\b': 'and so on',
        r'\bvs\.\b': 'versus',
        r'\be\.g\b': 'for example',
        r'\bi\.e\b': 'that is',
        r'\bdr\.': 'doctor',
        r'\bmr\.': 'mister',
        r'\bmrs\.': 'missus',
        r'\bms\.': 'miss',
        r'\bprof\.': 'professor',
        r'\bincl\.': 'including',
        r'\besq\.': 'esquire',
    }

    for pattern, replacement in abbreviations.items():
        audio_text = re.sub(pattern, replacement, audio_text, flags=re.IGNORECASE)

    # Step 7: Clean up whitespace
    # Remove multiple spaces
    audio_text = re.sub(r' +', ' ', audio_text)
    # Remove multiple line breaks, replace with single space or period
    audio_text = re.sub(r'\n\n+', '. ', audio_text)
    audio_text = re.sub(r'\n+', ' ', audio_text)

    # Step 8: Remove any remaining special characters that TTS might read literally
    # Keep only alphanumeric, spaces, periods, commas, colons, semicolons, quotes, hyphens
    audio_text = re.sub(r'[^\w\s\.\,\:\;\"\'\-]', '', audio_text)

    # Final cleanup
    audio_text = audio_text.strip()
    audio_text = re.sub(r'\s+', ' ', audio_text)

    return audio_text


def clean_for_audio(text: str) -> str:
    """Clean text for TTS audio generation.

    IMPORTANT: Use create_audio_transcript() instead for markdown summaries!
    This function is kept for backward compatibility.
    """
    return create_audio_transcript(text)


def format_for_display(text: str) -> str:
    """Format text for HTML display.

    Keep it clean and readable.
    """
    # Strip markdown formatting
    text = strip_markdown(text)

    # Clean up spacing
    text = re.sub(r'\s+', ' ', text)

    # Convert paragraphs (double newlines) to proper spacing
    text = re.sub(r'\n\n+', '\n\n', text)

    return text.strip()


if __name__ == "__main__":
    # Test
    sample = """# Getting Started with Python

- **Learn variables**: Assign values using Python 3.8
- **Data types**: integers (numbers), strings (text), Booleans (true/false)
- **Lists**: Create lists by enclosing values in brackets
- **Operators**: e.g., addition (+), subtraction (-), etc.

Here's an example: `print("hello")` which outputs text.

(Note: This is a beginner's guide.)"""

    print("Original (Markdown):")
    print(sample)
    print("\n" + "="*60)

    print("\nFor Display (with markdown):")
    print(format_for_display(sample))
    print("\n" + "="*60)

    print("\nFor Audio (plain text only):")
    print(create_audio_transcript(sample))
