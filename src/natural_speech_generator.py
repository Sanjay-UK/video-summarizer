"""Format text for natural-sounding TTS with proper structure.

Uses paragraph/sentence structure (no markup tags) to create natural speech.
Edge TTS automatically adds pacing based on punctuation and structure.
"""

import re
from pathlib import Path


def analyze_text_structure(text: str) -> dict:
    """Analyze text structure - paragraphs, sentences, lists.

    Returns:
        dict with structure information
    """
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]

    sentences_per_para = []
    for para in paragraphs:
        sentences = re.split(r'(?<=[.!?])\s+', para)
        sentences = [s.strip() for s in sentences if s.strip()]
        sentences_per_para.append(len(sentences))

    has_lists = bool(re.search(r'^\s*[-*+•]\s+|\d+\.\s+', text, re.MULTILINE))

    return {
        'num_paragraphs': len(paragraphs),
        'sentences_per_para': sentences_per_para,
        'has_lists': has_lists,
    }


def format_lists_for_audio(text: str) -> str:
    """Convert bullet/numbered lists to simple text format.

    Changes:
    - **Point** (bold) → Just Point
    - - item → Item 1: item
    - 1. item → Item 1: item
    """
    lines = text.split('\n')
    result = []
    list_counter = 0
    in_list = False

    for line in lines:
        # Detect bullet points
        if re.match(r'^\s*[-*+•]\s+', line):
            if not in_list:
                in_list = True
                list_counter = 0
            list_counter += 1
            # Remove bullet, keep content
            content = re.sub(r'^\s*[-*+•]\s+', '', line).strip()
            # Remove bold markers
            content = re.sub(r'\*\*(.+?)\*\*', r'\1', content)
            result.append(f"Item {list_counter}: {content}")
        # Detect numbered lists
        elif re.match(r'^\s*\d+\.\s+', line):
            in_list = True
            match = re.match(r'^\s*\d+\.\s+(.*)', line)
            if match:
                list_counter += 1
                content = match.group(1).strip()
                # Remove bold markers
                content = re.sub(r'\*\*(.+?)\*\*', r'\1', content)
                result.append(f"Item {list_counter}: {content}")
        else:
            in_list = False
            if line.strip():
                result.append(line.strip())

    return '\n'.join(result)


def clean_formatting(text: str) -> str:
    """Remove all markdown and special formatting.

    Keeps only plain text, proper punctuation, and structure.
    """
    # Remove bold (**text** → text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    # Remove italic (*text* → text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    # Remove underline (__text__ → text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    # Remove code blocks
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Remove inline code (`code` → code, but keep content)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Remove links but keep text [text](url) → text
    text = re.sub(r'\[(.+?)\]\([^)]+\)', r'\1', text)

    return text


def structure_for_natural_speech(text: str) -> str:
    """Structure text for natural TTS pacing.

    Edge TTS naturally pauses at:
    - Periods, question marks, exclamation marks
    - Paragraph breaks (double newlines)
    - Commas in sentences

    We just need to preserve and enhance these natural boundaries.
    """
    # Step 1: Clean all formatting
    text = clean_formatting(text)

    # Step 2: Format lists properly
    text = format_lists_for_audio(text)

    # Step 3: Ensure proper spacing
    # One space after periods
    text = re.sub(r'\.(\s+)', r'. ', text)
    # One space after commas
    text = re.sub(r',(\s+)', r', ', text)
    # Remove extra spaces
    text = re.sub(r' {2,}', ' ', text)

    # Step 4: Ensure paragraph breaks (double newline = natural pause)
    # Split by current double newlines and rejoin
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    text = '\n\n'.join(paragraphs)

    return text.strip()


def create_natural_audio_transcript(text: str) -> str:
    """Create natural audio transcript with proper text structure.

    NO XML/SSML TAGS - just clean text with proper formatting.
    Edge TTS handles pacing naturally based on punctuation and structure.

    Args:
        text: Clean plain text (markdown already removed)

    Returns:
        Well-structured plain text ready for TTS
    """
    # Analyze structure
    structure = analyze_text_structure(text)

    print(f"[AUDIO] Text analysis:")
    print(f"  - Paragraphs: {structure['num_paragraphs']}")
    print(f"  - Has lists: {structure['has_lists']}")

    # Structure for natural speech
    natural_text = structure_for_natural_speech(text)

    return natural_text


if __name__ == "__main__":
    # Test example
    sample_text = """The algorithm works in three main steps.

First, it validates the input data by checking for null values and type compatibility. Second, it processes each item through a transformation function that normalizes the data. Finally, it returns the results as a structured output.

Important: This function assumes valid input format. The process typically completes in linear time.

Implementation details: Use iteration for loop handling, not recursion. Key parameters include batch_size for controlling memory usage and timeout for preventing infinite loops."""

    print("Original text:")
    print(sample_text)
    print("\n" + "="*60 + "\n")

    print("Natural SSML output:")
    ssml = create_natural_audio_transcript(sample_text)
    print(ssml)
    print("\n" + "="*60 + "\n")

    print("Structure analysis:")
    structure = analyze_text_structure(sample_text)
    for key, value in structure.items():
        if key != 'paragraphs':
            print(f"  {key}: {value}")
