"""Domain detection and symbol handling for professional TTS audio.

Handles code, mathematics, chemistry, and physics content with appropriate
symbol pronunciation rules for each domain.
"""

from enum import Enum
from typing import Dict


class Domain(Enum):
    """Educational content domains"""
    CODE = 'code'
    MATH = 'math'
    CHEMISTRY = 'chemistry'
    PHYSICS = 'physics'
    GENERAL = 'general'


class DomainHandler:
    """Detect content domain and apply domain-specific pronunciation rules"""

    # Keywords that indicate each domain
    KEYWORDS = {
        'code': [
            'const', 'function', 'var', 'let', 'class', 'def', 'import',
            'export', 'return', 'if', 'else', 'for', 'while', 'method',
            'syntax', 'algorithm', 'variable', 'loop', 'conditional',
            '=>', '==', '!=', '++', '--'
        ],
        'math': [
            'equation', 'formula', 'solve', 'calculate', 'expression',
            'variable', 'coefficient', 'exponent', 'integral', 'derivative',
            'sum', 'squared', 'cubed', 'square root', 'function',
            '√', '∑', '∏', '^', 'π', '∫'
        ],
        'chemistry': [
            'element', 'compound', 'molecule', 'reaction', 'atom', 'bond',
            'oxidation', 'reduction', 'equation', 'H2O', 'CO2', 'NaCl',
            '→', '↔', 'yields', 'stoichiometry', 'equilibrium'
        ],
        'physics': [
            'force', 'energy', 'velocity', 'acceleration', 'momentum',
            'power', 'motion', 'constant', 'particle', 'mechanics',
            'thermodynamics', 'F=ma', 'E=mc', 'work', 'joule', 'newton'
        ],
    }

    # Symbol pronunciation rules for each domain
    SYMBOL_RULES = {
        'code': {
            '==': 'equals equals',
            '!=': 'not equals',
            '===': 'strict equals',
            '!==': 'strict not equals',
            '=>': 'arrow',
            '->': 'arrow',
            '++': 'increment',
            '--': 'decrement',
            '+=': 'plus equals',
            '-=': 'minus equals',
            '*=': 'multiply equals',
            '/=': 'divide equals',
            '&&': 'logical AND',
            '||': 'logical OR',
            '>>': 'right shift',
            '<<': 'left shift',
            '&': 'ampersand',
            '|': 'pipe',
            '^': 'caret',
            '~': 'tilde',
            '%': 'modulo',
        },
        'math': {
            '=': 'equals',
            '√': 'square root',
            '∑': 'summation',
            '∏': 'product',
            '∫': 'integral',
            '∂': 'partial derivative',
            'π': 'pi',
            'π': 'pi',
            '∞': 'infinity',
            '≈': 'approximately equals',
            '≤': 'less than or equal to',
            '≥': 'greater than or equal to',
            '≠': 'not equal to',
            '^': 'to the power of',
            '∝': 'proportional to',
            '∈': 'element of',
            '∪': 'union',
            '∩': 'intersection',
        },
        'chemistry': {
            '→': 'yields',
            '←': 'leftward arrow',
            '↔': 'reversible reaction',
            '⇌': 'reversible reaction',
            '+': 'plus',
            '=': 'yields',
            '(s)': 'solid',
            '(l)': 'liquid',
            '(g)': 'gas',
            '(aq)': 'aqueous',
            '∆': 'delta',
        },
        'physics': {
            '=': 'equals',
            '∝': 'proportional to',
            'F': 'force',
            'm': 'mass',
            'a': 'acceleration',
            'v': 'velocity',
            't': 'time',
            'p': 'momentum',
            'E': 'energy',
            'W': 'work',
            'P': 'power',
        },
    }

    @staticmethod
    def detect_domain(text: str) -> Domain:
        """Detect content domain based on keyword scoring.

        Args:
            text: Summary text to analyze

        Returns:
            Domain enum value indicating the detected content type
        """
        text_lower = text.lower()
        scores = {}

        for domain_name, keywords in DomainHandler.KEYWORDS.items():
            score = sum(text_lower.count(keyword.lower()) for keyword in keywords)
            scores[domain_name] = score

        # Return domain with highest score, default to general
        if max(scores.values()) == 0:
            return Domain.GENERAL

        max_domain = max(scores, key=scores.get)
        return Domain[max_domain.upper()]

    @staticmethod
    def convert_symbols(text: str, domain: Domain) -> str:
        """Convert domain-specific symbols to pronunciations.

        Args:
            text: Text to process
            domain: Domain enum value

        Returns:
            Text with symbols replaced by pronunciations
        """
        if domain == Domain.GENERAL:
            return text

        rules = DomainHandler.SYMBOL_RULES.get(domain.value, {})

        for symbol, pronunciation in rules.items():
            # Replace symbol with pronunciation, preserving word boundaries
            text = text.replace(f' {symbol} ', f' {pronunciation} ')

        return text

    @staticmethod
    def process_for_audio(text: str) -> str:
        """Full pipeline: detect domain and convert symbols.

        Args:
            text: Plain text (markdown already stripped)

        Returns:
            Text ready for TTS with domain-specific pronunciations
        """
        domain = DomainHandler.detect_domain(text)
        text = DomainHandler.convert_symbols(text, domain)
        return text


if __name__ == "__main__":
    # Test the handler
    test_cases = [
        ("const x = a + b; defines a variable", Domain.CODE),
        ("Solve the equation √(x² + 4) = 10", Domain.MATH),
        ("The reaction H2O + CO2 → H2CO3 forms carbonic acid", Domain.CHEMISTRY),
        ("Calculate force using F = ma where m is mass", Domain.PHYSICS),
    ]

    for text, expected_domain in test_cases:
        detected = DomainHandler.detect_domain(text)
        processed = DomainHandler.process_for_audio(text)
        print(f"Domain: {detected.value} (expected: {expected_domain.value})")
        print(f"Original:  {text}")
        print(f"Processed: {processed}")
        print()
