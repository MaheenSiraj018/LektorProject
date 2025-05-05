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


class ChoiceSource:
    def __init__(self, env, options):
        source = options.get("source")
        if source is not None:
            self.source = Expression(env, source)
            self.choices = None
            item_key = options.get("item_key") or "{{ this._id }}"
            item_label = options.get("item_label")
        else:
            self.source = None
            self.choices = _parse_choices(options)
            item_key = options.get("item_key") or "{{ this.0 }}"
            item_label = options.get("item_label")
        self.item_key = FormatExpression(env, item_key)
        if item_label is not None:
            item_label = FormatExpression(env, item_label)
        self.item_label = item_label

    @property
    def has_choices(self):
        return self.source is not None or self.choices is not None

    def iter_choices(self, pad, record=None, alt=PRIMARY_ALT):
        values = {}
        if record is not None:
            values["record"] = record
        if self.choices is not None:
            iterable = self.choices
        else:
            try:
                iterable = self.source.evaluate(pad, alt=alt, values=values)
            except Exception:
                traceback.print_exc()
                iterable = ()

        for item in iterable or ():
            key = self.item_key.evaluate(pad, this=item, alt=alt, values=values)
            if self.item_label is not None:
                label = {
                    "en": self.item_label.evaluate(
                        pad, this=item, alt=alt, values=values
                    )
                }
            else:
                if isinstance(item, (tuple, list)) and len(item) == 2:
                    label = item[1]
                elif hasattr(item, "get_record_label_i18n"):
                    label = item.get_record_label_i18n()
                else:
                    label = {"en": item["_id"]}

            yield key, label


class MultiType(Type):
    def __init__(self, env, options):
        Type.__init__(self, env, options)
        self.source = ChoiceSource(env, options)

    def get_labels(self, pad, record=None, alt=PRIMARY_ALT):
        return dict(self.source.iter_choices(pad, record, alt))

    def to_json(self, pad, record=None, alt=PRIMARY_ALT):
        rv = Type.to_json(self, pad, record, alt)
        if self.source.has_choices:
            rv["choices"] = list(self.source.iter_choices(pad, record, alt))
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


# 🔍 New Class Extension: ChoiceMetadataLogger
class ChoiceMetadataLogger:
    def __init__(self, choice_source):
        self.choice_source = choice_source

    def count_choices(self, pad, record=None, alt=PRIMARY_ALT):
        return len(list(self.choice_source.iter_choices(pad, record, alt)))

    def list_languages(self, pad, record=None, alt=PRIMARY_ALT):
        languages = set()
        for _, label_dict in self.choice_source.iter_choices(pad, record, alt):
            languages.update(label_dict.keys())
        return list(languages)

    def describe_choices(self, pad, record=None, alt=PRIMARY_ALT):
        lines = []
        for key, label_dict in self.choice_source.iter_choices(pad, record, alt):
            langs = ", ".join(f"{lang}: {label}" for lang, label in label_dict.items())
            lines.append(f"{key} => {langs}")
        return "\n".join(lines)
