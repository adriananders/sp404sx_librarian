#!/usr/bin/env python

# Description:
#  Parses a pattern from a Roland SP-404SX SD card and creates a MIDI file and SoundFont file.

# Usage:
#  ./ptn2midi.py SD_ROOT PATTERN_NAME TEMPO
#  Where...
#   SD_ROOT is the path (with trailing slash) to the top-level of the Roland SD card e.g. '/media/tz/SP-404SX/'
#   PATTERN_NAME is the name of the pattern e.g. 'a1'

# Output:
#  PTN_F1.mid
#  PTN_F1.sf2

import argparse
import importlib
import os
import os.path
import pysf
import shutil
import struct
import sys
import wave
import xml.etree.ElementTree as ElementTree
from collections import namedtuple
from datetime import datetime

from midiutil.MidiFile import MIDIFile
from pydub import AudioSegment

freepatstools = importlib.import_module("freepats-tools")
date = datetime.today().strftime('%Y-%m-%d')
sd_root_help = "The path (with trailing slash) to the top-level of the Roland SD card e.g. '/media/tz/SP-404SX/'"
argument_description = "Parses a pattern from a Roland SP-404SX SD card and creates a MIDI file and SoundFont file."
parser = argparse.ArgumentParser(
    description=argument_description)
parser.add_argument('SD_ROOT',
                    help=sd_root_help)
parser.add_argument('PATTERN_NAME', help="The name of the pattern e.g. 'a1'")
parser.add_argument('TEMPO', help="The tempo in beats per minute e.g. '95'")
parser.add_argument('SAMPLE_FORMAT', help="Sample format - WAV or AIFF")
if len(sys.argv) < 4:
    parser.print_help()
    sys.exit(1)
args = parser.parse_args()

TOTAL_BANKS = 10
PADS_PER_BANK = 12
PPQ = 96  # ubuntu saucy's python-midiutil 0.87-3 has TICKS_PER_BEAT==128
# see also https://code.google.com/p/midiutil/source/detail?spec=svn18&r=11
PADINFO_PATH = 'ROLAND/SP-404SX/SMPL/PAD_INFO.BIN'
PATTERN_DIRECTORY = 'ROLAND/SP-404SX/PTN/'
SAMPLE_DIRECTORY = 'ROLAND/SP-404SX/SMPL/'
BYTES_PER_NOTE = 8


# pad number (eg 13) to file name (eg "B0000001.WAV")
def pad_number_to_filename(pad_number, sampleformat):
    pad_number -= 1
    bank_number = int(pad_number / PADS_PER_BANK)
    bank_name = chr(ord('A') + bank_number)
    bank_pad_number = (pad_number % PADS_PER_BANK) + 1
    return bank_name + ('%07d' % bank_pad_number) + "." + sampleformat


assert (pad_number_to_filename(1, 'WAV') == 'A0000001.WAV')
assert (pad_number_to_filename(120, 'WAV') == 'J0000012.WAV')


# pattern name (eg B12) to pattern file name (eg PTN00012.BIN)
def pattern_name_to_filename(pattern_name):
    x = (ord(pattern_name[0].upper()) - ord('A')) * 12
    y = int(pattern_name[1:]) % PADS_PER_BANK
    return 'PTN' + str(x + y).zfill(5) + '.BIN'


assert (pattern_name_to_filename("A1") == 'PTN00001.BIN')
assert (pattern_name_to_filename("B11") == 'PTN00023.BIN')


# parse settings of each pad
def get_pad_info(path):
    # http://sp-forums.com/viewtopic.php?p=60548&sid=840a92a45a7790dd9b593f061ffb4478#p60548
    # http://sp-forums.com/viewtopic.php?p=60553#p60553
    pad_list = ['start',
                'end',
                'user_start',
                'user_end',
                'volume',
                'lofi',
                'loop',
                'gate',
                'reverse',
                'unknown1',
                'channels',
                'tempo_mode',
                'tempo',
                'user_tempo']
    Pad = namedtuple('Pad', " ".join(pad_list))
    f = open(path + PADINFO_PATH, 'rb')
    pads = {}
    i = 0
    while i < TOTAL_BANKS * PADS_PER_BANK:
        pad_data = f.read(32)
        pad = Pad._make(struct.unpack('>IIIIB????BBBII', pad_data))
        pads[i + 1] = pad
        i += 1
    return pads


# parse pattern
def get_pattern(path, pattern):
    # http://sp-forums.com/viewtopic.php?p=60635&sid=820f29eed0f7275dbeaf776173911736#p60635
    # http://sp-forums.com/viewtopic.php?p=60693&sid=820f29eed0f7275dbeaf776173911736#p60693
    Note = namedtuple('Note', 'delay pad bank_switch unknown2 velocity unknown3 length')
    f = open(path + PATTERN_DIRECTORY + pattern_name_to_filename(pattern), 'rb')
    ptn_filesize = os.fstat(f.fileno()).st_size
    notes = []
    i = 0
    while i < (ptn_filesize / BYTES_PER_NOTE) - 2:  # 2*8 trailer bytes at the end of the file
        note_data = f.read(8)
        note = Note._make(struct.unpack('>BBBBBBH', note_data))
        notes.append(note)

        i += 1
    #ptn_trailer = f.read(16) - not currently used
    #ptn_bars = ptn_trailer[9] - not currently used
    return notes


def notetuple_to_note_filename(note, sampleformat):
    return pad_number_to_filename(notetuple_to_sample_number(note), sampleformat)


def notetuple_to_sample_number(note):
    if note.bank_switch == 64 or note.bank_switch == 0:
        sample_number = note.pad - 46
    elif note.bank_switch == 65 or note.bank_switch == 1:
        sample_number = note.pad - 46 + PADS_PER_BANK * 5
    else:
        print("unexpected value for bank_switch")
        sys.exit(1)

    return sample_number


def padtuple_to_trim_samplenums(pad):
    return (pad.user_start - 512) / 2, (pad.user_end - 512) / 2


def create_midi_file(pads, notes, midi_tempo, path, pattern, sampleformat):
    midi_file = MIDIFile(numTracks=1)
    midi_file.addTrackName(track=0, time=0, trackName="Roland SP404SX Pattern " + pattern.upper() + " " + date)
    midi_file.addTempo(track=0, time=0, tempo=midi_tempo)
    note_path_to_pitch = {}
    # for C1. see "midi note numbers" in http://www.sengpielaudio.com/calculator-notenames.htm
    next_available_pitch = 36
    wave_table_list = []
    path_list = []
    time_in_beats_for_next_note = 0
    for note in notes:
        if note.pad != 128:
            note_filename = notetuple_to_note_filename(note, sampleformat)
            note_path = path + SAMPLE_DIRECTORY + note_filename
            wave_table_list.append(note_filename)
            path_list.append(note_path)
            if note_path not in note_path_to_pitch:
                note_path_to_pitch[note_path] = next_available_pitch
                next_available_pitch += 1
            if os.path.isfile(note_path):
                pad = pads[notetuple_to_sample_number(note)]
                user_start_sample, user_end_sample = padtuple_to_trim_samplenums(pad)
                outfile_path = "/tmp/" + os.path.basename(note_path)
                trim_wav_by_frame_numbers(note_path, outfile_path, user_start_sample, user_end_sample)
                stereo_to_mono(outfile_path, outfile_path + "_mono.wav")
                length = note.length / (PPQ * 1.0)
                midi_file.addNote(track=0, channel=0, pitch=note_path_to_pitch[note_path],
                                  time=time_in_beats_for_next_note, duration=length, volume=100)
            else:
                print("skipping missing sample")
        else:
            print("skipping empty note")
        delay = note.delay / (PPQ * 1.0)
        print("incrementing time by", delay)
        time_in_beats_for_next_note += delay

    # j = 36
    # while True:

    for i in note_path_to_pitch:
        template_wav_path = "template" + ('%02d' % (note_path_to_pitch[i] - 35)) + ".wav"
        trimmed_mono_path = "/tmp/" + os.path.basename(i) + "_mono.wav"
        if os.path.isfile(i):
            shutil.copyfile(trimmed_mono_path, template_wav_path)
        else:
            print("skipping missing sample wav")

    binfile = open("PTN_" + pattern.upper() + ".mid", 'wb')
    midi_file.writeFile(binfile)
    binfile.close()
    return wave_table_list, path_list


# play it with "timidity output.mid" /etc/timidity/freepats.cfg
# see eg /usr/share/midi/freepats/Tone_000/004_Electric_Piano_1_Rhodes.pat

# via http://ubuntuforums.org/showthread.php?t=1882580
def trim_wav_by_frame_numbers(infile_path, outfile_path, start_frame, end_frame):
    in_file = wave.open(infile_path, "r")
    out_file = wave.open(outfile_path, "w")
    out_length_frames = int(end_frame - start_frame)
    out_file.setparams((in_file.getnchannels(), in_file.getsampwidth(), in_file.getframerate(), out_length_frames,
                        in_file.getcomptype(), in_file.getcompname()))
    in_file.setpos(start_frame)
    out_file.writeframes(in_file.readframes(out_length_frames))


def stereo_to_mono(infile_path, outfile_path):
    sound = AudioSegment.from_wav(infile_path)
    sound = sound.set_channels(1)
    sound.export(outfile_path, format="wav")


def create_template(pattern, wave_table_list, path_list):
    instrument_name = "PTN_" + pattern.upper() + " " + date
    begin_key = 36
    end_key = begin_key + len(wave_table_list) - 1
    key_value = begin_key
    wave_table_id = 1
    zones = []
    wavetables = []
    xml_data = ElementTree.Element('sf:pysf')
    xml_data.set('xmlns:sf', '.')
    xml_data.set('xmlns:xsi', 'http://www.w3.org/2001/XMLSchema-instance')
    xml_data.set('version', '3')
    xml_data.set('xsi:schemaLocation', '.')
    sf2 = ElementTree.SubElement(xml_data, 'sf2')
    ElementTree.SubElement(sf2, 'ICRD').text = date
    ifil = ElementTree.SubElement(sf2, 'IFIL')
    major = ElementTree.SubElement(ifil, 'major')
    minor = ElementTree.SubElement(ifil, 'minor')
    major.text = "2"
    minor.text = "1"
    ElementTree.SubElement(sf2, 'INAM').text = "PySF"
    ElementTree.SubElement(sf2, 'IPRD').text = "SBAWE32"
    ElementTree.SubElement(sf2, 'ISFT').text = "PySF"
    ElementTree.SubElement(sf2, 'ISNG').text = "PySF"
    instruments = ElementTree.SubElement(sf2, 'instruments')
    instrument = ElementTree.SubElement(instruments, 'instrument')
    ElementTree.SubElement(instrument, 'id').text = "1"
    ElementTree.SubElement(instrument, 'name').text = instrument_name
    instrument_zones = ElementTree.SubElement(instrument, 'zones')
    presets = ElementTree.SubElement(sf2, 'presets')
    preset = ElementTree.SubElement(presets, 'preset')
    ElementTree.SubElement(preset, 'bank').text = "128"
    ElementTree.SubElement(preset, 'id').text = "1"
    ElementTree.SubElement(preset, 'name').text = instrument_name
    preset_zones = ElementTree.SubElement(preset, 'zones')
    preset_zone = ElementTree.SubElement(preset_zones, 'zone')
    ElementTree.SubElement(preset_zone, 'instrumentId').text = "1"
    key_range = ElementTree.SubElement(preset_zone, 'keyRange')
    ElementTree.SubElement(key_range, 'begin').text = str(begin_key)
    ElementTree.SubElement(key_range, 'end').text = str(end_key)
    wave_tables = ElementTree.SubElement(sf2, 'wavetables')

    for wave_table in wave_table_list:
        wave_table_name = wave_table.replace(".wav", "").replace(".aiff", "")

        instrument_zone = ElementTree.SubElement(instrument_zones, 'zone')
        instrument_key_range = ElementTree.SubElement(instrument_zone, 'keyRange')

        ElementTree.SubElement(instrument_key_range, 'begin').text = str(key_value)
        ElementTree.SubElement(instrument_key_range, 'end').text = str(key_value)
        ElementTree.SubElement(instrument_zone, 'overridingRootKey').text = str(key_value)
        ElementTree.SubElement(instrument_zone, 'sampleModes').text = '0_LoopNone'
        ElementTree.SubElement(instrument_zone, 'wavetableId').text = str(wave_table_id)

        wave_table_data = ElementTree.SubElement(wave_tables, 'wavetable')
        ElementTree.SubElement(wave_table_data, 'file').text = path_list[wave_table_id - 1]
        ElementTree.SubElement(wave_table_data, 'id').text = str(wave_table_id)
        loop = ElementTree.SubElement(wave_table_data, 'loop')
        ElementTree.SubElement(loop, 'begin').text = "1"
        ElementTree.SubElement(loop, 'end').text = "1"
        ElementTree.SubElement(wave_table_data, 'name').text = wave_table_name
        key_value = key_value + 1
        wave_table_id = wave_table_id + 1

    with open("/tmp/pysftemplate.xml", "w") as file:
        file.write("<?xml version=\"1.0\" ?>" + ElementTree.tostring(xml_data).decode("utf-8"))


def create_soundfont_file(pattern):
    pysf.XmlToSf("/tmp/pysftemplate.xml", "PTN_" + pattern.upper() + ".sf2")


def parsepath(path):
    if path[:-1] != "/":
        path = path + "/"
    return path


if __name__ == "__main__":
    pattern_tempo = int(sys.argv[3])
    file_path = parsepath(sys.argv[1])
    pattern_data = sys.argv[2]
    sample_format = sys.argv[4]
    pads_data = get_pad_info(file_path)
    notes_data = get_pattern(file_path, pattern_data)
    wavetable_data, path_data = create_midi_file(pads_data,
                                                 notes_data,
                                                 pattern_tempo,
                                                 file_path,
                                                 pattern_data,
                                                 sample_format)
    create_template(pattern_data, wavetable_data, path_data)
    create_soundfont_file(pattern_data)
