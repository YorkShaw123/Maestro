"""Bounded source inference shared by the AI rule scanner and branch view."""
from dataclasses import dataclass
import re

NUM = r'[+-]?[0-9]{1,9}'
STATE = re.compile(
    r'StateNo\s*(!=|=)\s*(?:(' + NUM + r')|([\[(])\s*(' + NUM +
    r')\s*,\s*(' + NUM + r')\s*([\])]))', re.I)
AI_LEVEL = re.compile(r'AILevel(?:\s*(?:>\s*0|>=\s*[1-8]|!=\s*0))?', re.I)
AI_COMMAND = re.compile(r'command\s*=\s*"(?:ai|cpu)(?:[_ -]?\d+|[_ -]?(?:on|start))?"', re.I)
VAR_TEST = re.compile(r'var\s*\(\s*(\d+)\s*\)\s*(?:(>=|>|=|!=)\s*(' + NUM + r'))?', re.I)
DIFFICULTY = re.compile(r'Random\s*(?:<=|<)\s*var\s*\(\s*(\d+)\s*\)\s*\*\s*[1-9][0-9]*', re.I)
BOOL_TARGET = re.compile(r'(' + NUM + r')\s*([+-])\s*\((.+)\)\s*(?:\*\s*(' + NUM + r'))?', re.I)
BOOL_MULT_FIRST = re.compile(r'(' + NUM + r')\s*([+-])\s*(' + NUM + r')\s*\*\s*\((.+)\)', re.I)
STATE_OFFSET = re.compile(r'StateNo(?:\s*([+-])\s*([0-9]{1,9}))?', re.I)
SINGLE_COMPARISON = re.compile(r'(?<![!:])(?:<=|>=|!=|=|<|>)')


def number(value):
    return int(value) if value and re.fullmatch(NUM, value) else None


def delimiters(text, separator):
    """Return top-level separator positions, preserving MUGEN range delimiters."""
    depth, quoted, escaped = 0, False, False
    for i, char in enumerate(text):
        if quoted:
            if char == '"' and not escaped:
                quoted = False
            escaped = char == '\\' and not escaped
        elif char == '"':
            quoted = True
        elif char in '([':
            depth += 1
        elif char in ')]':
            depth -= 1
        elif depth == 0 and text.startswith(separator, i):
            yield i


def split_top(text, separator):
    start, parts = 0, []
    for end in delimiters(text, separator):
        parts.append(text[start:end].strip())
        start = end+len(separator)
    parts.append(text[start:].strip())
    return parts


def unwrap(text):
    text = text.strip()
    while text.startswith('(') and text.endswith(')'):
        inner = text[1:-1]
        # If the first pair ends early, its closing bracket is at top level inside inner.
        balance = 0
        quoted = escaped = False
        for char in inner:
            if quoted:
                if char == '"' and not escaped:
                    quoted = False
                escaped = char == '\\' and not escaped
            elif char == '"':
                quoted = True
            elif char in '([':
                balance += 1
            elif char in ')]':
                balance -= 1
                if balance < 0:
                    break
        if balance != 0:
            break
        text = inner.strip()
    return text


def guarantees(text, atom):
    text = unwrap(text)
    alternatives = split_top(text, '||')
    if len(alternatives) > 1:
        return all(guarantees(part, atom) for part in alternatives)
    required = split_top(text, '&&')
    if len(required) > 1:
        return any(guarantees(part, atom) for part in required)
    return atom(text)


def boolean_atoms(text):
    text = unwrap(text)
    for separator in ('||', '&&'):
        parts = split_top(text, separator)
        if len(parts) > 1:
            for part in parts:
                yield from boolean_atoms(part)
            return
    yield text


def trigger_groups(triggers):
    common, groups = [], {}
    for key, value, line in triggers:
        if key == 'triggerall':
            common.append((key, value, line))
        else:
            groups.setdefault(key, []).append((key, value, line))
    return common, tuple(groups.values())


def trigger_guarantees(triggers, test):
    common, groups = trigger_groups(triggers)

    def has(entries):
        return any(guarantees(value, test) for _, value, _ in entries)

    return has(common) or bool(groups) and all(has(group) for group in groups)


def positive_variable(text, variables, *, allow_nonzero=True):
    difficulty = DIFFICULTY.fullmatch(text)
    if difficulty:
        return int(difficulty[1]) in variables
    match = VAR_TEST.fullmatch(text)
    if not match or int(match[1]) not in variables:
        return False
    _, op, value = match.groups()
    if op is None:
        return allow_nonzero
    value = int(value)
    return (allow_nonzero and op == '!=' and value == 0 or op == '=' and value > 0 or
            op == '>' and value >= 0 or op == '>=' and value > 0)


@dataclass(frozen=True)
class StateFacts:
    # None means the expression leaves the state unconstrained.
    allowed: frozenset | None = None
    excluded: frozenset = frozenset()
    anchors: frozenset = frozenset()

    def permits(self, state):
        return (self.allowed is None or state in self.allowed) and state not in self.excluded


def union(facts):
    allowed = None if any(f.allowed is None for f in facts) else frozenset().union(*(f.allowed for f in facts))
    # A state is ruled out by OR only when all branches rule it out.
    possible_exclusions = frozenset().union(*(f.excluded for f in facts))
    excluded = frozenset(n for n in possible_exclusions if not any(f.permits(n) for f in facts))
    return StateFacts(allowed, excluded, frozenset().union(*(f.anchors for f in facts)))


def combine(facts):
    allowed, excluded, anchors = None, frozenset(), frozenset()
    for fact in facts:
        if fact.allowed is not None:
            allowed = fact.allowed if allowed is None else allowed & fact.allowed
        excluded |= fact.excluded
        anchors |= fact.anchors
    return StateFacts(allowed, excluded, frozenset(n for n in anchors if (allowed is None or n in allowed) and n not in excluded))


def state_facts(text, states):
    text = unwrap(text)
    or_parts = split_top(text, '||')
    if len(or_parts) > 1:
        return union(tuple(state_facts(part, states) for part in or_parts))
    and_parts = split_top(text, '&&')
    if len(and_parts) > 1:
        return combine(state_facts(part, states) for part in and_parts)
    negated = text.startswith('!')
    match = STATE.fullmatch(unwrap(text[1:]) if negated else text)
    if not match:
        return StateFacts()
    op, exact, left, low, high, right = match.groups()
    if exact:
        found = frozenset({int(exact)})
    else:
        start = int(low) + (left == '(')
        stop = int(high) - (right == ')')
        # Index declared states only. Large hit-state ranges cannot create thousands of fake branches.
        found = frozenset(item for item in states if start <= item <= stop)
        if not found:
            return StateFacts()
    if negated == (op == '!='):
        return StateFacts(allowed=found, anchors=found)
    return StateFacts(excluded=found)


def state_groups(triggers, states):
    common, groups = trigger_groups(triggers)
    general = combine(state_facts(value, states) for _, value, _ in common)
    alternatives = []
    for group in groups or ([],):
        facts = combine((general, *(state_facts(value, states) for _, value, _ in group)))
        alternatives.append((facts, group))
    return common, alternatives


def variants(expression, conditions=(), depth=0, *, source=None):
    """Return explicit possible target arms, leaving dynamic residuals intact."""
    expression = unwrap(expression or '')
    result = number(expression)
    if result is not None or depth >= 5:
        return [(result, conditions)]
    if source is not None and (own_state := STATE_OFFSET.fullmatch(expression)):
        sign, offset = own_state.groups()
        delta = int(offset) if offset else 0
        return [(source-delta if sign == '-' else source+delta, conditions)]
    call = re.fullmatch(r'(?:ifelse|cond)\s*\((.*)\)', expression, re.I)
    if call:
        parts = split_top(call[1], ',')
        if len(parts) >= 3:
            # Redirection in the condition can occupy a comma as well.
            test = ', '.join(parts[:-2])
            yes, no = parts[-2:]
            return (variants(yes, (*conditions, (True, test)), depth+1, source=source) +
                    variants(no, (*conditions, (False, test)), depth+1, source=source))
    offset = BOOL_TARGET.fullmatch(expression)
    first = BOOL_MULT_FIRST.fullmatch(expression)
    if offset or first:
        if offset:
            base, sign, test, multiplier = offset.groups()
        else:
            base, sign, multiplier, test = first.groups()
        test = unwrap(test)
        parts = re.split(r'<=|>=|!=|=|<|>', test)
        if (len(parts) == 2 and not any(ch in test for ch in ('&&', '||', ':=')) and
                len(SINGLE_COMPARISON.findall(test)) == 1):
            delta = int(multiplier) if multiplier else 1
            return [(int(base)+(delta if sign == '+' else -delta), (*conditions, (True, test))),
                    (int(base), (*conditions, (False, test)))]
    return [(None, conditions)]
