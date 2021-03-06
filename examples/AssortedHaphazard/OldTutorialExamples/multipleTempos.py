#  ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++  #
#  This file is part of SCAMP (Suite for Computer-Assisted Music in Python)                      #
#  Copyright © 2020 Marc Evanstein <marc@marcevanstein.com>.                                     #
#                                                                                                #
#  This program is free software: you can redistribute it and/or modify it under the terms of    #
#  the GNU General Public License as published by the Free Software Foundation, either version   #
#  3 of the License, or (at your option) any later version.                                      #
#                                                                                                #
#  This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;     #
#  without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.     #
#  See the GNU General Public License for more details.                                          #
#                                                                                                #
#  You should have received a copy of the GNU General Public License along with this program.    #
#  If not, see <http://www.gnu.org/licenses/>.                                                   #
#  ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++  #

from scamp import *
from math import sin
import random

session = Session()

flute = session.new_part("flute")
clarinet = session.new_part("clarinet")
bassoon = session.new_part("bassoon")


def flute_part(clock: Clock):
    clock.set_tempo_targets((160, 160, 100, 100, 130, 130, 70, 70), (0, 1, 1, 2, 2, 3, 3, 4), loop=True)
    while True:
        flute.play_note(int(70 + 10 * clock.rate), 0.8, 0.25, "staccato")


def clarinet_part(clock: Clock):
    clock.apply_tempo_function(lambda t: 60 + 30 * sin(t), duration_units="time")
    while True:
        clarinet.play_note(int(65 + (clock.rate - 1) * 20 + random.random() * 8), 0.8, 0.25, "staccato")


def bassoon_part(clock: Clock):
    clock.apply_tempo_function(lambda t: 80 + 40 * sin(t / 3), duration_units="time")
    while True:
        bassoon.play_chord([40, 44, 50], 0.8, 0.5, "staccatissimo")


flute_clock = session.fork(flute_part, name="Flute")
clarinet_clock = session.fork(clarinet_part, name="Clarinet")
bassoon_clock = session.fork(bassoon_part, name="Bassoon")


performance1 = session.start_transcribing(clock=flute_clock)
performance2 = session.start_transcribing(clock=clarinet_clock)
performance3 = session.start_transcribing(clock=bassoon_clock)

session.wait(30)
session.stop_transcribing(performance1).quantized().to_score(title="Recorded on flute clock").show_xml()
# session.stop_transcribing(performance2).quantized().to_score(title="Recorded on clarinet clock").show_xml()
# session.stop_transcribing(performance3).quantized().to_score(title="Recorded on bassoon clock").show_xml()
