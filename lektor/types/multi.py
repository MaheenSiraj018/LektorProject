import traceback
from lektor.constants import PRIMARY_ALT
from lektor.environment.expressions import Expression
from lektor.environment.expressions import FormatExpression
from lektor.i18n import get_i18n_block
from lektor.types.base import Type


def _reflow_and_split_labels(labels):
    rv = []
    for lang, string in labels.items():
        for idx, item in enumerate(string.split(",")):
            try:
                d = rv[idx]
            except LookupError:
                d = {}
                rv.append(d)
            d[lang] = item.strip()
    return rv


def _parse_choices(options):
    s = options.get("choices")
    if not s:
        return None

    choices = []
    items = s.split(",")
    user_labels = get_i18n_block(options, "choice_labels")
    implied_labels = []

    for item in items:
        if "=" in item:
            choice, value = item.split("=", 1)
            choice = choice.strip()
            if choice.isdigit():
                choice = int(choice)
            implied_labels.append(value.strip())
            choices.append(choice)
        else:
            choices.append(item.strip())
            implied_labels.append(item.strip())

    if user_labels:
        rv = list(zip(choices, _reflow_and_split_labels(user_labels)))
    else:
        rv = [(key, {"en": label}) for key, label in zip(choices, implied_labels)]

    return rv


class MultiType(Type):
    def _init_(self, env, options):
        Type._init_(self, env, options)

        # Removing the need for Strategy Pattern here by collapsing the
        # previously used ChoiceSource-based logic directly into MultiType
        self.static_choices = options.get("choices")
        self.source = options.get("source")

        # Directly using the fields instead of delegating to other classes
        self.item_key = FormatExpression(env, options.get("item_key") or "{{ this._id }}")
        item_label = options.get("item_label")
        self.item_label = FormatExpression(env, item_label) if item_label else None

    def iter_choices(self, pad, record=None, alt=PRIMARY_ALT):
        values = {}
        if record is not None:
            values["record"] = record
        if self.static_choices:
            # Static choices (from "choices" field)
            iterable = self.static_choices.split(",")
        elif self.source:
            # Dynamic choices (from the "source" expression)
            try:
                iterable = self.source.evaluate(pad, alt=alt, values=values)
            except Exception:
                traceback.print_exc()
                iterable = []

        else:
            iterable = []

        for item in iterable:
            key = self.item_key.evaluate(pad, this=item, alt=alt, values=values)
            if self.item_label:
                label = {"en": self.item_label.evaluate(pad, this=item, alt=alt, values=values)}
            else:
                label = {"en": item}

            yield key, label

    def get_labels(self, pad, record=None, alt=PRIMARY_ALT):
        return dict(self.iter_choices(pad, record, alt))

    def to_json(self, pad, record=None, alt=PRIMARY_ALT):
        rv = Type.to_json(self, pad, record, alt)
        if self.static_choices or self.source:
            rv["choices"] = list(self.iter_choices(pad, record, alt))
        return rv


class SelectType(MultiType):
    widget = "select"

    def value_from_raw(self, raw):
        if raw.value is None:
            return raw.missing_value("Missing select value")
        return raw.value


class CheckboxesType(MultiType):
    widget = "checkboxes"

    def value_from_raw(self, raw):
        rv = [x.strip() for x in (raw.value or "").split(",")]
        if rv == [""]:
            rv = []
        return rv
