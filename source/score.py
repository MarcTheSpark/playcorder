from scamp.settings import quantization_settings, engraving_settings
from scamp.envelope import Envelope
from scamp.quantization import QuantizationRecord, QuantizationScheme
from scamp.performance_note import PerformanceNote
from scamp.utilities import get_standard_indispensability_array, prime_factor, floor_x_to_pow_of_y
from scamp.engraving_translations import notehead_name_to_lilypond_type
from scamp.note_properties import NotePropertiesDictionary
import math
from fractions import Fraction
from itertools import permutations
import textwrap
import abjad
from collections import namedtuple
from abc import ABC, abstractmethod
import logging

# TODO:
# - looking through for situations like tied eighths on and of 1 and 2 combining into quarters


# ---------------------------------------------- Duration Utilities --------------------------------------------

length_to_note_type = {
    8.0: "breve",
    4.0: "whole",
    2.0: "half",
    1.0: "quarter",
    0.5: "eighth",
    0.25: "16th",
    1.0/8: "32nd",
    1.0/16: "64th",
    1.0/32: "128th"
}


def get_basic_length_and_num_dots(length):
    length = Fraction(length).limit_denominator()
    if length in length_to_note_type:
        return length, 0
    else:
        dots_multiplier = 1.5
        dots = 1
        while length / dots_multiplier not in length_to_note_type:
            dots += 1
            dots_multiplier = (2.0 ** (dots + 1) - 1) / 2.0 ** dots
            if dots > engraving_settings.max_dots_allowed:
                raise ValueError("Duration length of {} does not resolve to single note type.".format(length))
        return length / dots_multiplier, dots


def is_single_note_length(length):
    try:
        get_basic_length_and_num_dots(length)
        return True
    except ValueError:
        return False


def length_to_undotted_constituents(length):
    # fix any floating point inaccuracies
    length = Fraction(length).limit_denominator()
    length_parts = []
    while length > 0:
        this_part = floor_x_to_pow_of_y(length, 2.0)
        length -= this_part
        length_parts.append(this_part)
    return length_parts


def _get_beat_division_indispensabilities(beat_length, beat_divisor):
    # In general, it's best to divide a beat into the smaller prime factors first. For instance, a 6 tuple is probably
    # easiest as two groups of 3 rather than 3 groups of 2. (This is definitely debatable and context dependent.)
    # An special case occurs when the beat naturally wants to divide a certain way. For instance, a beat of length 1.5
    # divided into 6 will prefer to divide into 3s first and then 2s.

    # first, get the divisor prime factors from to small
    divisor_factors = sorted(prime_factor(beat_divisor))

    # then get the natural divisors of the beat length from big to small
    natural_factors = sorted(prime_factor(Fraction(beat_length).limit_denominator().numerator), reverse=True)

    # now for each natural factor
    for natural_factor in natural_factors:
        # if it's a factor of the divisor
        if natural_factor in divisor_factors:
            # then pop it and move it to the front
            divisor_factors.pop(divisor_factors.index(natural_factor))
            divisor_factors.insert(0, natural_factor)
            # (Note that we sorted the natural factors from big to small so that the small ones get
            # pushed to the front last and end up at the very beginning of the queue)

    return get_standard_indispensability_array(divisor_factors, normalize=True)


# ---------------------------------------------- Score Classes --------------------------------------------


class ScoreContainer(ABC):

    def __init__(self, contents, contents_argument_name, allowable_child_types, extra_field_names=()):
        self._contents = contents if contents is not None else []
        self._contents_argument_name = contents_argument_name
        self._extra_field_names = extra_field_names
        self._allowable_child_types = allowable_child_types
        assert isinstance(self._contents, list) and all(isinstance(x, allowable_child_types) for x in self._contents)

    def __contains__(self, item):
        return item in self._contents

    def __delitem__(self, i):
        del self._contents[i]

    def __getitem__(self, argument):
        return self._contents.__getitem__(argument)

    def __iter__(self):
        return iter(self._contents)

    def __len__(self):
        return len(self._contents)

    def __setitem__(self, i, item):
        assert isinstance(item, self._allowable_child_types), "Incompatible child type"
        self._contents[i] = item

    def append(self, item):
        assert isinstance(item, self._allowable_child_types), "Incompatible child type"
        self._contents.append(item)

    def extend(self, items):
        assert hasattr(items, "__len__")
        assert all(isinstance(item, self._allowable_child_types) for item in items), "Incompatible child type"
        self._contents.extend(items)

    def index(self, item):
        return self._contents.index(item)

    def insert(self, index, item):
        assert isinstance(item, self._allowable_child_types), "Incompatible child type"
        return self._contents.insert(index, item)

    def pop(self, i=-1):
        return self._contents.pop(i)

    def remove(self, item):
        return self._contents.remove(item)

    def __repr__(self):
        extra_args_string = "" if not hasattr(self, "_extra_field_names") \
            else ", ".join("{}={}".format(x, self.__dict__[x]) for x in self._extra_field_names)
        if len(extra_args_string) > 0:
            extra_args_string += ", "
        contents_string = "\n" + textwrap.indent(",\n".join(str(x) for x in self._contents), "   ") + "\n" \
            if len(self._contents) > 0 else ""
        return "{}({}{}=[{}])".format(
            self.__class__.__name__,
            extra_args_string,
            self._contents_argument_name,
            contents_string
        )


class ScoreComponent(ABC):

    # used when we're rendering a full score: this can go outside the lilypond context
    outer_stemless_def = """
% Definition to improve score readability
stemless = {
    \once \override Beam.stencil = ##f
    \once \override Flag.stencil = ##f
    \once \override Stem.stencil = ##f
}"""

    # used when we're rendering something smaller than a score: this version can go inside scores / measures / etc.
    inner_stemless_def = r"""% Definition to improve score readability
    #(define stemless 
        (define-music-function (parser location)
            ()
            #{
                \once \override Beam.stencil = ##f
                \once \override Flag.stencil = ##f
                \once \override Stem.stencil = ##f
            #})
        )
    """

    gliss_overrides = [
        r"% Make the glisses a little thicker, make sure they have at least a little length, and allow line breaks",
        r"\override Score.Glissando.minimum-length = #4",
        r"\override Score.Glissando.springs-and-rods = #ly:spanner::set-spacing-rods",
        r"\override Score.Glissando.thickness = #2",
        r"\override Score.Glissando #'breakable = ##t",
        "\n"
    ]

    @abstractmethod
    def _to_abjad(self):
        """
        Convert this to an abjad representation
        :return: the abjad translation of this score component, possibly missing needed definitions and overrides
        """
        pass

    @abstractmethod
    def to_music_xml(self):
        pass

    def to_abjad(self):
        """
        This wrapper around the _to_abjad implementation details makes sure we incorporate the appropriate definitions
        :return: the abjad translation of this score component, ready to show
        """
        assert abjad is not None, "Abjad is required for this operation."
        abjad_object = self._to_abjad()
        lilypond_code = format(abjad_object)
        if r"\glissando" in lilypond_code:
            for gliss_override in ScoreComponent.gliss_overrides:
                abjad.attach(abjad.LilyPondLiteral(gliss_override), abjad_object, "opening")

        if r"\stemless" in lilypond_code:
            abjad.attach(abjad.LilyPondLiteral(ScoreComponent.inner_stemless_def), abjad_object, "opening")

        return abjad_object

    def to_abjad_lilypond_file(self, title=None, composer=None):
        assert abjad is not None, "Abjad is required for this operation."
        abjad_object = self._to_abjad()
        lilypond_code = format(abjad_object)

        if r"\glissando" in lilypond_code:
            for gliss_override in ScoreComponent.gliss_overrides:
                abjad.attach(abjad.LilyPondLiteral(gliss_override), abjad_object, "opening")

        abjad_lilypond_file = abjad.LilyPondFile.new(
            music=abjad_object
        )

        # if we're actually producing the lilypond file itself, then we put the simpler
        # definition of stemless outside of the main score object.
        if r"\stemless" in lilypond_code:
            abjad_lilypond_file.items.insert(-1, ScoreComponent.outer_stemless_def)

        if title is not None:
            abjad_lilypond_file.header_block.title = abjad.Markup(title)
        if composer is not None:
            abjad_lilypond_file.header_block.composer = abjad.Markup(composer)

        return abjad_lilypond_file

    def export_lilypond(self, file_path):
        title = self.title if hasattr(self, "title") else None
        composer = self.composer if hasattr(self, "composer") else None
        with open(file_path, "w") as output_file:
            output_file.write(format(self.to_abjad_lilypond_file(title, composer)))

    def to_lilypond(self, wrap_as_file=False):
        title = self.title if hasattr(self, "title") else None
        composer = self.composer if hasattr(self, "composer") else None
        assert abjad is not None, "Abjad is required for this operation."
        return format(self.to_abjad_lilypond_file(title, composer) if wrap_as_file else self.to_abjad())

    def print_lilypond(self, wrap_as_file=False):
        print(self.to_lilypond(wrap_as_file=wrap_as_file))

    def show(self):
        assert abjad is not None, "Abjad is required for this operation."
        # we use a lilypond file wrapper if we need to display title or composer info (i.e. for Scores)
        title = self.title if hasattr(self, "title") else None
        composer = self.composer if hasattr(self, "composer") else None
        abjad.show(self.to_abjad_lilypond_file(title=title, composer=composer)
                   if title is not None or composer is not None else self.to_abjad())


class Score(ScoreComponent, ScoreContainer):

    def __init__(self, parts=None, title=None, composer=None):
        ScoreContainer.__init__(self, parts, "parts", (StaffGroup, Staff), ("title", "composer"))
        self.title = title
        self.composer = composer

    @property
    def parts(self):
        return self._contents

    @property
    def staves(self):
        # returns all the staves of all the parts in the score
        out = []
        for part in self.parts:
            if isinstance(part, StaffGroup):
                out.extend(part.staves)
            else:
                assert isinstance(part, Staff)
                out.append(part)
        return out

    @classmethod
    def from_performance(cls, performance, quantization_scheme=None, title="default", composer="default"):
        if not performance.is_quantized() and quantization_scheme is None:
            # not quantized and no quantization scheme given, so we need to pick a default quantization scheme
            logging.warning("No quantization scheme given; quantizing according to default time signature")
            quantization_scheme = QuantizationScheme.from_time_signature(quantization_settings.default_time_signature)

        return Score.from_quantized_performance(
            performance if quantization_scheme is None else performance.quantized(quantization_scheme),
            title=title, composer=composer
        )

    @classmethod
    def from_quantized_performance(cls, performance, title="default", composer="default"):
        assert performance.is_quantized(), "Performance was not quantized."
        contents = []
        for part in performance.parts:
            if engraving_settings.ignore_empty_parts and part.num_measures() == 0:
                # if this is an empty part, and we're not including empty parts, skip it
                continue
            staff_group = StaffGroup.from_quantized_performance_part(part)
            if len(staff_group.staves) > 1:
                contents.append(staff_group)
            elif len(staff_group.staves) == 1:
                contents.append(staff_group.staves[0])
        out = cls(
            contents,
            title=engraving_settings.get_default_title() if title == "default" else title,
            composer=engraving_settings.get_default_composer() if composer == "default" else composer
        )
        if engraving_settings.pad_incomplete_parts:
            out.pad_incomplete_parts()
        return out

    def pad_incomplete_parts(self):
        staves = self.staves
        longest_staff = max(staves, key=lambda staff: len(staff.measures))
        longest_staff_length = len(longest_staff.measures)
        for staff in self.staves:
            while len(staff.measures) < longest_staff_length:
                corresponding_measure_in_long_staff = longest_staff.measures[len(staff.measures)]
                staff.measures.append(Measure.empty_measure(corresponding_measure_in_long_staff.time_signature,
                                                            corresponding_measure_in_long_staff.show_time_signature))

    def _to_abjad(self):
        score = abjad.Score([part._to_abjad() for part in self.parts])
        return score

    def to_music_xml(self):
        pass


# used in arranging voices in a part
_NumberedVoiceFragment = namedtuple("_NumberedVoiceFragment", "voice_num start_measure_num measures_with_quantizations")
_NamedVoiceFragment = namedtuple("_NamedVoiceFragment", "average_pitch start_measure_num measures_with_quantizations")


class StaffGroup(ScoreComponent, ScoreContainer):

    def __init__(self, staves):
        ScoreContainer.__init__(self, staves, "staves", Staff)

    @property
    def staves(self):
        return self._contents

    @classmethod
    def from_quantized_performance_part(cls, quantized_performance_part):
        assert quantized_performance_part.is_quantized()

        fragments = StaffGroup._separate_voices_into_fragments(quantized_performance_part)
        measure_voice_grid = StaffGroup._create_measure_voice_grid(fragments, quantized_performance_part.num_measures())
        return StaffGroup.from_measure_voice_grid(
            measure_voice_grid, quantized_performance_part.get_longest_quantization_record()
        )

    @staticmethod
    def _construct_voice_fragment(voice_name, notes, start_measure_num, measure_quantizations):
        average_pitch = sum(note.average_pitch() for note in notes) / len(notes)

        # split the notes into measures, breaking notes that span a barline in two
        # save each to measures_with_quantizations, in a tuple along with the corresponding quantization
        measures_with_quantizations = []
        for measure_quantization in measure_quantizations:
            measure_end_time = measure_quantization.start_time + measure_quantization.measure_length
            this_measure_notes = []
            remaining_notes = []
            for note in notes:
                # check if the note starts in the measure
                if measure_quantization.start_time <= note.start_time < measure_end_time:
                    # check if it straddles the following barline
                    if note.end_time > measure_end_time:
                        first_half, second_half = note.split_at_beat(measure_end_time)
                        this_measure_notes.append(first_half)
                        remaining_notes.append(second_half)
                    else:
                        # note is fully within the measure
                        this_measure_notes.append(note)
                else:
                    # if it happens in a later measure, save it for later
                    remaining_notes.append(note)
            notes = remaining_notes
            measures_with_quantizations.append((this_measure_notes, measure_quantization))

        # then decide based on the name of the voice whether it is from a numbered voice, which gets treated differently
        try:
            # numbered voice
            voice_num = int(voice_name)
            return _NumberedVoiceFragment(voice_num - 1, start_measure_num, measures_with_quantizations)
        except ValueError:
            # not a numbered voice, so we want to order voices mostly by pitch
            return _NamedVoiceFragment(average_pitch, start_measure_num, measures_with_quantizations)

    @staticmethod
    def _separate_voices_into_fragments(quantized_performance_part):
        """
        Splits the part's voices into fragments where divisions occur whenever there is a measure break at a rest.
        If there's a measure break but not a rest, we're probably in the middle of a melodic gesture, so don't want to
        separate. If there's a rest but not a measure break then we should also probably keep the notes together in a
        single voice, since they were specified to be in the same voice.
        :param quantized_performance_part: a quantized PerformancePart
        :return: a tuple of (numbered_fragments, named_fragments), where the numbered_fragments come from numbered voices
        and are of the form (voice_num, notes_list, start_measure_num, end_measure_num, measure_quantization_schemes),
        while the named_fragments are of the form (notes_list, start_measure_num, end_measure_num,
        measure_quantization_schemes)
        """
        fragments = []

        for voice_name, note_list in quantized_performance_part.voices.items():
            # first we make an enumeration iterator for the measures
            if len(note_list) == 0:
                continue

            quantization_record = quantized_performance_part.voice_quantization_records[voice_name]
            assert isinstance(quantization_record, QuantizationRecord)
            measure_quantization_iterator = enumerate(quantization_record.quantized_measures)

            # the idea is that we build a current_fragment up until we encounter a rest at a barline
            # when that happens, we save the old fragment and start a new one
            current_fragment = []
            fragment_measure_quantizations = []
            current_measure_num, current_measure = next(measure_quantization_iterator)
            fragment_start_measure = 0

            for performance_note in note_list:
                # update so that current_measure is the measure that performance_note starts in
                while performance_note.start_time >= current_measure.start_time + current_measure.measure_length:
                    # we're past the old measure, so increment to next measure
                    current_measure_num, current_measure = next(measure_quantization_iterator)
                    # if this measure break coincides with a rest, then we start a new fragment
                    if len(current_fragment) > 0 and current_fragment[-1].end_time < performance_note.start_time:
                        fragments.append(StaffGroup._construct_voice_fragment(
                            voice_name, current_fragment, fragment_start_measure, fragment_measure_quantizations
                        ))
                        # reset all the fragment-building variables
                        current_fragment = []
                        fragment_measure_quantizations = []
                        fragment_start_measure = current_measure_num
                    elif len(current_fragment) == 0:
                        # don't mark the start measure until we actually have a note!
                        fragment_start_measure = current_measure_num

                # add the new note to the current fragment
                current_fragment.append(performance_note)

                # make sure that fragment_measure_quantizations has a copy of the measure this note starts in
                if len(fragment_measure_quantizations) == 0 or fragment_measure_quantizations[-1] != current_measure:
                    fragment_measure_quantizations.append(current_measure)

                # now we move forward to the end of the note, and update the measure we're on
                # (Note the > rather than a >= sign. For the end of the note, it has to actually cross the barline.)
                while performance_note.end_time > current_measure.start_time + current_measure.measure_length:
                    current_measure_num, current_measure = next(measure_quantization_iterator)
                    # when we cross into a new measure, add it to the measure quantizations
                    fragment_measure_quantizations.append(current_measure)

            # once we're done going through the voice, save the last fragment and move on
            if len(current_fragment) > 0:
                fragments.append(StaffGroup._construct_voice_fragment(
                    voice_name, current_fragment, fragment_start_measure, fragment_measure_quantizations)
                )

        return fragments

    @staticmethod
    def _create_measure_voice_grid(fragments, num_measures):
        numbered_fragments = []
        named_fragments = []
        while len(fragments) > 0:
            fragment = fragments.pop()
            if isinstance(fragment, _NumberedVoiceFragment):
                numbered_fragments.append(fragment)
            else:
                named_fragments.append(fragment)

        measure_grid = [[] for _ in range(num_measures)]

        def is_cell_free(which_measure, which_voice):
            return len(measure_grid[which_measure]) <= which_voice or measure_grid[which_measure][which_voice] is None

        # sort by measure number (i.e. fragment[2]) then by voice number (i.e. fragment[0])
        numbered_fragments.sort(key=lambda frag: (frag.start_measure_num, frag.voice_num))
        # sort by measure number, then by highest to lowest pitch, then by longest to shortest fragment
        named_fragments.sort(key=lambda frag: (frag.start_measure_num, -frag.average_pitch,
                                               -len(frag.measures_with_quantizations)))

        for fragment in numbered_fragments:
            assert isinstance(fragment, _NumberedVoiceFragment)
            measure_num = fragment.start_measure_num
            for measure_with_quantization in fragment.measures_with_quantizations:
                while len(measure_grid[measure_num]) <= fragment.voice_num:
                    measure_grid[measure_num].append(None)
                measure_grid[measure_num][fragment.voice_num] = measure_with_quantization
                measure_num += 1

        for fragment in named_fragments:
            assert isinstance(fragment, _NamedVoiceFragment)
            measure_range = range(fragment.start_measure_num,
                                  fragment.start_measure_num + len(fragment.measures_with_quantizations))
            voice_num = 0
            while not all(is_cell_free(measure_num, voice_num) for measure_num in measure_range):
                voice_num += 1

            measure_num = fragment.start_measure_num
            for measure_with_quantization in fragment.measures_with_quantizations:
                while len(measure_grid[measure_num]) <= voice_num:
                    measure_grid[measure_num].append(None)
                measure_grid[measure_num][voice_num] = measure_with_quantization
                measure_num += 1

        return measure_grid

    @classmethod
    def from_measure_voice_grid(cls, measure_bins, quantization_record):
        """
        Creates a StaffGroup with Staves that accommodate engraving_settings.max_voices_per_part voices each
        :param measure_bins: a list of voice lists (can be many voices each)
        :param quantization_record: a QuantizationRecord
        """
        num_staffs_required = 1 if len(measure_bins) == 0 else \
            int(max(math.ceil(len(x) / engraving_settings.max_voices_per_part) for x in measure_bins))

        # create a bunch of dummy bins for the different measures of each staff
        #             measures ->      staffs -v
        # [ [None, None, None, None, None, None, None, None],
        #   [None, None, None, None, None, None, None, None] ]
        staves = [[None] * len(measure_bins) for _ in range(num_staffs_required)]

        for measure_num, measure_voices in enumerate(measure_bins):
            # this breaks up the measure's voices into groups of length max_voices_per_part
            # (the last group might have fewer)
            voice_groups = [measure_voices[i:i + engraving_settings.max_voices_per_part]
                            for i in range(0, len(measure_voices), engraving_settings.max_voices_per_part)]

            for staff_num in range(len(staves)):
                # for each staff, check if this measure has enough voices to even reach that staff
                if staff_num < len(voice_groups):
                    # if so, let's take a look at our voices for this measure
                    this_voice_group = voice_groups[staff_num]
                    if all(x is None for x in this_voice_group):
                        # if all the voices are empty, this staff is empty for this measure. Put None to indicate that
                        staves[staff_num][measure_num] = None
                    else:
                        # otherwise, there's something there, so put tne voice group in the slot
                        staves[staff_num][measure_num] = this_voice_group
                else:
                    # if not, put None there to indicate an empty measure
                    staves[staff_num][measure_num] = None

        # At this point, each entry in the staves / measures matrix is either
        #   (1) None, indicating an empty measure
        #   (2) a list of voices, each of which is either:
        #       - a list of PerformanceNotes or
        #       - None, in the case of an empty voice

        if all(len(x) == 0 for x in staves):
            # empty staff group; none of its staves have any contents
            return cls([Staff([])])
        return cls([Staff.from_measure_bins_of_voice_lists(x, quantization_record.time_signatures) for x in staves])

    def _to_abjad(self):
        return abjad.StaffGroup([staff._to_abjad() for staff in self.staves])

    def to_music_xml(self):
        pass


def _join_same_source_abjad_note_group(same_source_group):
    # look pairwise to see if we need to tie or gliss
    # sometimes a note will gliss, then sit at a static pitch

    gliss_present = False
    for note_pair in zip(same_source_group[:-1], same_source_group[1:]):
        if isinstance(note_pair[0], abjad.Note) and note_pair[0].written_pitch == note_pair[1].written_pitch or \
                isinstance(note_pair[0], abjad.Chord) and note_pair[0].written_pitches == note_pair[1].written_pitches:
            abjad.tie(abjad.Selection(note_pair))
            # abjad.attach(abjad.Tie(), abjad.Selection(note_pair))
        else:
            # abjad.glissando(abjad.Selection(note_pair))
            abjad.attach(abjad.LilyPondLiteral("\glissando", "after"), note_pair[0])

            # abjad.attach(abjad.Glissando(), abjad.Selection(note_pair))
            gliss_present = True

    if gliss_present:
        # if any of the segments gliss, we might attach a slur
        abjad.slur(abjad.Selection(same_source_group))
        # abjad.attach(abjad.Slur(), abjad.Selection(same_source_group))


class Staff(ScoreComponent, ScoreContainer):

    def __init__(self, measures):
        ScoreContainer.__init__(self, measures, "measures", Measure)

    @property
    def measures(self):
        return self._contents

    @classmethod
    def from_measure_bins_of_voice_lists(cls, measure_bins, time_signatures):
        # Expects a list of measure bins formatted as outputted by StaffGroup.from_measure_bins_of_voice_lists
        #   (1) None, indicating an empty measure
        #   (2) a list of voices, each of which is either:
        #       - a list of PerformanceNotes or
        #       - None, in the case of an empty voice
        time_signature_changes = [True] + [time_signatures[i - 1] != time_signatures[i]
                                           for i in range(1, len(time_signatures))]
        return cls([Measure.from_list_of_performance_voices(measure_content, time_signature, show_time_signature)
                    if measure_content is not None else Measure.empty_measure(time_signature, show_time_signature)
                    for measure_content, time_signature, show_time_signature in zip(measure_bins, time_signatures,
                                                                                    time_signature_changes)])

    def _to_abjad(self):
        # from the point of view of the source_id_dict (which helps us connect tied notes), the staff is
        # always going to be the top level call. There's no need to propagate the source_id_dict any further upward
        source_id_dict = {}
        contents = [measure._to_abjad(source_id_dict) for measure in self.measures]
        for same_source_group in source_id_dict.values():
            _join_same_source_abjad_note_group(same_source_group)

        return abjad.Staff(contents)

    def to_music_xml(self):
        pass


_voice_names = [r'voiceOne', r'voiceTwo', r'voiceThree', r'voiceFour']
_voice_literals= [r'\voiceOne', r'\voiceTwo', r'\voiceThree', r'\voiceFour']


class Measure(ScoreComponent, ScoreContainer):

    def __init__(self, voices, time_signature, show_time_signature=True):
        ScoreContainer.__init__(self, voices, "voices", (Voice, type(None)), ("time_signature", "show_time_signature"))
        self.time_signature = time_signature
        self.show_time_signature = show_time_signature

    @property
    def voices(self):
        return self._contents

    @classmethod
    def empty_measure(cls, time_signature, show_time_signature=True):
        return cls([Voice.empty_voice(time_signature)], time_signature, show_time_signature=show_time_signature)

    @classmethod
    def from_list_of_performance_voices(cls, voices_list, time_signature, show_time_signature=True):
        # voices_list consists of elements each of which is either:
        #   - a (list of PerformanceNotes, measure quantization record) tuple for an active voice
        #   - None, for an empty voice
        if all(voice_content is None for voice_content in voices_list):
            # if all the voices are empty, just make an empty measure
            return cls.empty_measure(time_signature, show_time_signature=show_time_signature)
        else:
            voices = []
            for i, voice_content in enumerate(voices_list):
                if voice_content is None:
                    if i == 0:
                        # an empty first voice should be expressed as a bar rest
                        voices.append(Voice.empty_voice(time_signature))
                    else:
                        # an empty other voice can just be ignored. Put a placeholder of None
                        voices.append(None)
                else:
                    # should be a (list of PerformanceNotes, measure quantization record) tuple
                    voices.append(Voice.from_performance_voice(*voice_content))
            return cls(voices, time_signature, show_time_signature=show_time_signature)

    def _to_abjad(self, source_id_dict=None):
        is_top_level_call = True if source_id_dict is None else False
        source_id_dict = {} if source_id_dict is None else source_id_dict
        abjad_measure = abjad.Container()
        for i, voice in enumerate(self.voices):
            if voice is None:
                continue
            abjad_voice = self.voices[i]._to_abjad(source_id_dict)

            if i == 0 and self.show_time_signature:
                # TODO: THIS SEEMS BROKEN IN ABJAD, SO I HAVE A KLUGEY FIX WITH A LITERAL
                # abjad.attach(self.time_signature.to_abjad(), abjad_voice[0])
                abjad.attach(abjad.LilyPondLiteral(r"\time {}".format(self.time_signature.as_string()), "opening"),
                             abjad_voice)
            if len(self.voices) > 1:
                abjad.attach(abjad.LilyPondLiteral(_voice_literals[i]), abjad_voice)
            abjad_voice.name = _voice_names[i]
            abjad_measure.append(abjad_voice)
        abjad_measure.is_simultaneous = True

        if is_top_level_call:
            for same_source_group in source_id_dict.values():
                _join_same_source_abjad_note_group(same_source_group)

        return abjad_measure

    def to_music_xml(self):
        pass


class Voice(ScoreComponent, ScoreContainer):

    def __init__(self, contents, time_signature):
        ScoreContainer.__init__(self, contents, "contents", (Tuplet, NoteLike), ("time_signature", ))
        self.time_signature = time_signature

    @property
    def contents(self):
        return self._contents

    @classmethod
    def empty_voice(cls, time_signature):
        return cls(None, time_signature)

    @classmethod
    def from_performance_voice(cls, notes, measure_quantization):
        """
        This is where a lot of the magic of converting performed notes to written symbols occurs.
        :param notes: the list of PerformanceNotes played in this measure
        :param measure_quantization: the quantization used for this measure for this voice
        :return: a Voice object containing all the notation
        """
        length = measure_quantization.measure_length

        # split any notes that have a tuple length into segments of those lengths
        notes = [segment for note in notes for segment in note.split_at_length_divisions()]

        # change each PerformanceNote to have a start_time relative to the start of the measure
        for note in notes:
            note.start_time -= measure_quantization.start_time

        notes = Voice._fill_in_rests(notes, length)
        # break notes that cross beat boundaries into two tied notes
        # later, some of these can be recombined, but we need to convert them to NoteLikes first

        notes = Voice._split_notes_at_beats(notes, [beat.start_time_in_measure for beat in measure_quantization.beats])

        # construct the processed contents of this voice (made up of NoteLikes Tuplets)
        processed_contents = []
        for beat_quantization in measure_quantization.beats:
            notes_from_this_beat = []

            while len(notes) > 0 and \
                    notes[0].start_time < beat_quantization.start_time_in_measure + beat_quantization.length:
                # go through all the notes in this beat
                notes_from_this_beat.append(notes.pop(0))

            processed_contents.extend(Voice._process_and_convert_beat(notes_from_this_beat, beat_quantization))

        # instantiate and return the constructed voice
        return cls(processed_contents, measure_quantization.time_signature)

    @staticmethod
    def _fill_in_rests(notes, total_length):
        notes_and_rests = []
        t = 0
        for note in notes:
            if t < note.start_time:
                notes_and_rests.append(PerformanceNote(t, note.start_time - t, None, None, {}))
            notes_and_rests.append(note)
            t = note.end_time

        if t < total_length:
            notes_and_rests.append(PerformanceNote(t, total_length - t, None, None, {}))
        return notes_and_rests

    @staticmethod
    def _split_notes_at_beats(notes, beats):
        for beat in beats:
            split_notes = []
            for note in notes:
                split_notes.extend(note.split_at_beat(beat))
            notes = split_notes
        return notes

    @staticmethod
    def _process_and_convert_beat(beat_notes, beat_quantization):
        beat_start_time = beat_notes[0].start_time

        # this covers the case in which a single voice was quantized, some notes overlapped so it had to be split in
        # two, the two voices were forced to share the same divisor, and one of them ended up empty for that voice
        divisor = None if all(note.pitch is None for note in beat_notes) else beat_quantization.divisor

        if divisor is None:
            # if there's no beat divisor, then it should just be a note or rest of the full length of the beat
            assert len(beat_notes) == 1
            pitch, length, properties = beat_notes[0].pitch, beat_notes[0].length, beat_notes[0].properties

            if is_single_note_length(length):
                return [NoteLike(pitch, length, properties)]
            else:
                constituent_lengths = length_to_undotted_constituents(length)
                return [NoteLike(pitch, l, properties) for l in constituent_lengths]

        # if the divisor requires a tuplet, we construct it
        tuplet = Tuplet.from_length_and_divisor(beat_quantization.length, divisor) if divisor is not None else None

        dilation_factor = 1 if tuplet is None else tuplet.dilation_factor()
        written_division_length = beat_quantization.length / divisor * dilation_factor

        division_indispensabilities = _get_beat_division_indispensabilities(beat_quantization.length, divisor)

        note_list = tuplet.contents if tuplet is not None else []

        for note in beat_notes:
            written_length = note.length * dilation_factor
            if is_single_note_length(written_length):
                written_length_components = [written_length]
            else:
                written_length_components = length_to_undotted_constituents(written_length)

            # try every permutation of the length constituents. Get a score for it by multiplying the length of
            # each constituent with the indispensability of that pulse within the beat and summing them.
            best_permutation = written_length_components
            best_score = 0

            for permutation in permutations(written_length_components):
                accumulated_length = note.start_time - beat_start_time
                division_indices = [int(round(accumulated_length / written_division_length))]
                for component_length in permutation[:-1]:
                    accumulated_length += component_length
                    division_indices.append(int(round(accumulated_length / written_division_length)))

                score = sum(segment_length * division_indispensabilities[division_index]
                            for division_index, segment_length in zip(division_indices, permutation))

                if score > best_score:
                    best_score = score
                    best_permutation = permutation

            note_parts = []
            remainder = note
            for segment_length in best_permutation:

                split_note = remainder.split_at_beat(remainder.start_time + segment_length / dilation_factor)
                if len(split_note) > 1:
                    this_segment, remainder = split_note
                else:
                    this_segment = split_note[0]

                note_parts.append(NoteLike(this_segment.pitch, segment_length, this_segment.properties))

            note_list.extend(note_parts)

        return [tuplet] if tuplet is not None else note_list

    def _to_abjad(self, source_id_dict=None):
        if len(self.contents) == 0:  # empty voice
            return abjad.Voice("{{R{}*{}}}".format(self.time_signature.denominator, self.time_signature.numerator))
        else:
            is_top_level_call = True if source_id_dict is None else False
            source_id_dict = {} if source_id_dict is None else source_id_dict
            abjad_components = [x._to_abjad(source_id_dict) for x in self.contents]
            if is_top_level_call:
                for same_source_group in source_id_dict.values():
                    _join_same_source_abjad_note_group(same_source_group)
            return abjad.Voice(abjad_components)

    def to_music_xml(self):
        pass


class Tuplet(ScoreComponent, ScoreContainer):

    def __init__(self, tuplet_divisions, normal_divisions, division_length, contents=None):
        """
        Creates a tuplet representing tuplet_divisions in the space of normal_divisions of division_length
        e.g. 7, 4, and 0.25 would mean '7 in the space of 4 sixteenth notes'
        """
        ScoreContainer.__init__(self, contents, "contents", NoteLike,
                                ("tuplet_divisions", "normal_divisions", "division_length"))
        self.tuplet_divisions = tuplet_divisions
        self.normal_divisions = normal_divisions
        self.division_length = division_length

    @property
    def contents(self):
        return self._contents

    def dilation_factor(self):
        return self.tuplet_divisions / self.normal_divisions

    def length(self):
        return self.normal_divisions * self.division_length

    def length_within_tuplet(self):
        return self.tuplet_divisions * self.division_length

    @classmethod
    def from_length_and_divisor(cls, length, divisor):
        # constructs the appropriate tuplet from the length and the divisor

        # consider a beat length of 1.5 and a tuplet of 11
        # normal_divisions gets set initially to 3 and normal type gets set to 8, since it's 3 eighth notes long
        beat_length_fraction = Fraction(length).limit_denominator()
        normal_divisions = beat_length_fraction.numerator
        # (if denominator is 1, normal type is quarter note, 2 -> eighth note, etc.)
        normal_type = 4 * beat_length_fraction.denominator

        # now, we keep dividing the beat in two until we're just about to divide it into more pieces than the divisor
        # so in our example, we start with 3 8th notes, then 6 16th notes, but we don't go up to 12 32nd notes, since
        # that is more than the beat divisor of 11. Now we know that we are looking at 11 in the space of 6 16th notes.
        while normal_divisions * 2 <= divisor:
            normal_divisions *= 2
            normal_type *= 2

        if normal_divisions == divisor:
            # if the beat divisor exactly equals the normal number, then we don't have a tuplet at all,
            # just a standard duple division. Return None to signify that
            return None
        else:
            # otherwise, construct a tuplet from our answer
            return cls(divisor, normal_divisions, 4.0 / normal_type)

    def _to_abjad(self, source_id_dict=None):
        is_top_level_call = True if source_id_dict is None else False
        source_id_dict = {} if source_id_dict is None else source_id_dict
        abjad_notes = [note_like._to_abjad(source_id_dict) for note_like in self.contents]
        if is_top_level_call:
            for same_source_group in source_id_dict.values():
                _join_same_source_abjad_note_group(same_source_group)
        return abjad.Tuplet(abjad.Multiplier(self.normal_divisions, self.tuplet_divisions), abjad_notes)

    def to_music_xml(self):
        pass


class NoteLike(ScoreComponent):

    def __init__(self, pitch, written_length, properties):
        """
        Represents note, chord, or rest that can be notated without ties
        :param pitch: tuple if a pitch, None if a rest
        """
        self.pitch = pitch
        self.written_length = written_length
        self.properties = properties if isinstance(properties, NotePropertiesDictionary) \
            else NotePropertiesDictionary.from_unknown_format(properties)

    @staticmethod
    def _get_relevant_gliss_control_points(pitch_envelope):
        """
        The idea here is that the control points that matter are the ones that aren't near others or an endpoint
        (temporal_relevance) and are a significant deviation in pitch from the assumed interpolated pitch if we
        didn't notate them (pitch_deviation).
        :param pitch_envelope: a pitch Envelope (gliss)
        :return: a list of the important control points
        """
        assert isinstance(pitch_envelope, Envelope)
        controls_to_check = pitch_envelope.times[1:-1] \
            if engraving_settings.glissandi.consider_non_extrema_control_points else pitch_envelope.local_extrema()

        relevant_controls = []
        left_bound = pitch_envelope.start_time()
        last_pitch = pitch_envelope.start_level()
        for control_point in controls_to_check:
            progress_to_endpoint = (control_point - left_bound) / (pitch_envelope.end_time() - left_bound)
            temporal_relevance = 1 - abs(0.5 - progress_to_endpoint) * 2
            # figure out how much the pitch at this control point deviates from just linear interpolation
            linear_interpolated_pitch = last_pitch + (pitch_envelope.end_level() - last_pitch) * progress_to_endpoint
            pitch_deviation = abs(pitch_envelope.value_at(control_point) - linear_interpolated_pitch)
            if temporal_relevance * pitch_deviation > engraving_settings.glissandi.inner_grace_relevance_threshold:
                relevant_controls.append(control_point)
                left_bound = control_point
                last_pitch = pitch_envelope.value_at(control_point)
        return relevant_controls

    def _to_abjad(self, source_id_dict=None):
        """
        Convert this NoteLike to an abjad note, chord, or rest, along with possibly some headless grace notes to
        represent important changes of direction in a glissando, if the glissando engraving setting are set to do so
        :param source_id_dict: a dictionary keeping track of which abjad notes come from the same original
        PerformanceNote. This is populated here when the abjad notes are generated, and then later, once a whole
        staff of notes has been generated, ties and glissandi are added accordingly.
        :return: an abjad note, chord, or rest, possibly with an attached AfterGraceContainer
        """
        # abjad duration
        duration = Fraction(self.written_length / 4).limit_denominator()
        # list of gliss grace notes, if applicable
        grace_notes = []

        if self.pitch is None:
            # Just a rest
            abjad_object = abjad.Rest(duration)
        elif isinstance(self.pitch, tuple):
            # This is a chord
            abjad_object = abjad.Chord()
            abjad_object.written_duration = duration

            # Now, is it a glissing chord?
            if isinstance(self.pitch[0], Envelope):
                # if so, its noteheads are based on the start level
                abjad_object.note_heads = [self.properties.spelling_policy.resolve_abjad_pitch(x.start_level())
                                           for x in self.pitch]
                # Set the notehead
                self._set_abjad_note_head_styles(abjad_object)
                last_pitches = abjad_object.written_pitches

                # if the glissando engraving settings say to do so, we'll include
                # relevant inner turn around points as headless grace notes
                grace_points = NoteLike._get_relevant_gliss_control_points(self.pitch[0]) \
                    if engraving_settings.glissandi.control_point_policy == "grace" else []

                # also, if this is the last segment of a quantized and split PerformanceNote, and if the glissando
                # engraving settings say to do so, we include the final pitch reached as a headless grace note
                if not self.properties.starts_tie() and engraving_settings.glissandi.include_end_grace_note:
                    grace_points += [self.pitch[0].end_time()]

                # add a grace chord for each important turn around point in the gliss
                for t in grace_points:
                    grace_chord = abjad.Chord()
                    grace_chord.written_duration = 1/16
                    grace_chord.note_heads = [self.properties.spelling_policy.resolve_abjad_pitch(x.value_at(t))
                                              for x in self.pitch]
                    # Set the notehead
                    self._set_abjad_note_head_styles(grace_chord)
                    # but first check that we're not just repeating the last grace chord
                    if grace_chord.written_pitches != last_pitches:
                        grace_notes.append(grace_chord)
                        last_pitches = grace_chord.written_pitches
            else:
                # if not, our job is simple
                abjad_object.note_heads = [self.properties.spelling_policy.resolve_abjad_pitch(x) for x in self.pitch]
                # Set the noteheads
                self._set_abjad_note_head_styles(abjad_object)

        elif isinstance(self.pitch, Envelope):
            # This is a note doing a glissando
            abjad_object = abjad.Note(self.properties.spelling_policy.resolve_abjad_pitch(self.pitch.start_level()),
                                      duration)
            # Set the notehead
            self._set_abjad_note_head_styles(abjad_object)
            last_pitch = abjad_object.written_pitch

            # if the glissando engraving settings say to do so, we'll include
            # relevant inner turn around points as headless grace notes
            grace_points = NoteLike._get_relevant_gliss_control_points(self.pitch) \
                if engraving_settings.glissandi.control_point_policy == "grace" else []

            # also, if this is the last segment of a quantized and split PerformanceNote, and if the glissando
            # engraving settings say to do so, we include the final pitch reached as a headless grace note
            if not self.properties.starts_tie() and engraving_settings.glissandi.include_end_grace_note:
                grace_points += [self.pitch.end_time()]

            for t in grace_points:
                grace = abjad.Note(self.properties.spelling_policy.resolve_abjad_pitch(self.pitch.value_at(t)), 1 / 16)
                # Set the notehead
                self._set_abjad_note_head_styles(grace)
                # but first check that we're not just repeating the last grace note pitch
                if last_pitch != grace.written_pitch:
                    grace_notes.append(grace)
                    last_pitch = grace.written_pitch
        else:
            # This is a simple note
            abjad_object = abjad.Note(self.properties.spelling_policy.resolve_abjad_pitch(self.pitch), duration)
            # Set the notehead
            self._set_abjad_note_head_styles(abjad_object)

        # Now we make, fill, and attach the abjad AfterGraceContainer, if applicable
        if len(grace_notes) > 0:
            for note in grace_notes:
                # this signifier, \stemless, is not standard lilypond, and is defined with
                # an override at the start of the score
                abjad.attach(abjad.LilyPondLiteral("\stemless"), note)
            grace_container = abjad.AfterGraceContainer(grace_notes)
            abjad.attach(grace_container, abjad_object)
            # TODO: THE FOLLOWING SHOULDN'T BE NECESSARY ONCE ABJAD FIXES THE AfterGraceContainer PROBLEM
            if isinstance(grace_notes[0], abjad.Chord):
                abjad.attach(abjad.LilyPondLiteral(r"\afterGrace"), abjad_object)
        else:
            grace_container = None

        # this is where we populate the source_id_dict passed down to us from the top level "to_abjad()" call
        if source_id_dict is not None:
            # sometimes a note will not have a _source_id property defined, since it never gets broken into tied
            # components. However, if it's a glissando and there's stemless grace notes involved, we're going to
            # have to give it a _source_id so that it can share it with its grace notes
            if grace_container is not None and "_source_id" not in self.properties:
                self.properties["_source_id"] = PerformanceNote.next_id()

            if "_source_id" in self.properties:
                # here we take the new note that we're creating and add it to the bin in source_id_dict that
                # contains all the notes of the same source, so that they can be tied / joined by glissandi
                if self.properties["_source_id"] in source_id_dict:
                    # this source_id is already associated with a leaf, so add it to the list
                    source_id_dict[self.properties["_source_id"]].append(abjad_object)
                else:
                    # we don't yet have a record on this source_id, so start a list with this object under that key
                    source_id_dict[self.properties["_source_id"]] = [abjad_object]

                # add any grace notes to the same bin as their parent
                if grace_container is not None:
                    source_id_dict[self.properties["_source_id"]].extend(grace_container)

        return abjad_object

    def _set_abjad_note_head_styles(self, abjad_note_or_chord):
        if isinstance(abjad_note_or_chord, abjad.Note):
            note_head_style = self.properties.noteheads[0]
            if note_head_style != "normal":
                abjad.tweak(abjad_note_or_chord.note_head).style = notehead_name_to_lilypond_type[note_head_style]
        elif isinstance(abjad_note_or_chord, abjad.Chord):
            for chord_member, note_head_style in enumerate(self.properties.noteheads):
                if note_head_style != "normal":
                    abjad.tweak(abjad_note_or_chord.note_heads[chord_member]).style = \
                        notehead_name_to_lilypond_type[note_head_style]
        else:
            raise ValueError("Must be an abjad Note or Chord object")

    def to_music_xml(self):
        pass

    def __repr__(self):
        return "NoteLike(pitch={}, written_length={}, properties={})".format(
            self.pitch, self.written_length, self.properties
        )
