#!/usr/bin/env python

# Description:
#  Parses a pattern from a Roland SP-404SX SD card and creates a MIDI file and SoundFont file.

# Usage:
#  ./ptn2midi.py SD_ROOT PATTERN_NAME TEMPO
#  Where...
#   SD_ROOT is the path (with trailing slash) to the top-level of the Roland SD card e.g. '/media/tz/SP-404SX/'
#   PATTERN_NAME is the name of the pattern e.g. 'a1'
#   TEMPO is the tempo in beats per minute e.g. '95'

# Output:
#  PTN_F1.mid
#  PTN_F1.sf2

import struct, binascii, sys, os, wave, os.path, shutil, pysf, argparse, importlib
import xml.etree.ElementTree as ET
from collections import namedtuple
from midiutil.MidiFile import MIDIFile
from pydub import AudioSegment
from datetime import datetime
freepatstools = importlib.import_module("freepats-tools")


parser = argparse.ArgumentParser(description="Parses a pattern from a Roland SP-404SX SD card and creates a MIDI file and SoundFont file.")
parser.add_argument('SD_ROOT', help="The path (with trailing slash) to the top-level of the Roland SD card e.g. '/media/tz/SP-404SX/'")
parser.add_argument('PATTERN_NAME', help="The name of the pattern e.g. 'a1'")
parser.add_argument('TEMPO', help="The tempo in beats per minute e.g. '95'")
if len(sys.argv) < 2:
	parser.print_help()
	sys.exit(1)
args = parser.parse_args()

TOTAL_BANKS = 10
PADS_PER_BANK = 12
PPQ = 96 # ubuntu saucy's python-midiutil 0.87-3 has TICKS_PER_BEAT==128
         # see also https://code.google.com/p/midiutil/source/detail?spec=svn18&r=11
PADINFO_PATH =      'ROLAND/SP-404SX/SMPL/PAD_INFO.BIN'
PATTERN_DIRECTORY = 'ROLAND/SP-404SX/PTN/'
SAMPLE_DIRECTORY =  'ROLAND/SP-404SX/SMPL/'
BYTES_PER_NOTE=8

# pad number (eg 13) to file name (eg "B0000001.WAV")
def pad_number_to_filename(pad_number):
	pad_number -= 1
	bank_number = int(pad_number / PADS_PER_BANK)
	bank_name = chr(ord('A') + bank_number)
	#print bank_name
	bank_pad_number = (pad_number % PADS_PER_BANK) + 1
	#print bank_pad_number
	return bank_name + ('%07d' % bank_pad_number) + ".WAV" # TODO: handle AIF

assert(pad_number_to_filename(1)=='A0000001.WAV')
assert(pad_number_to_filename(120)=='J0000012.WAV')

# pattern name (eg B12) to pattern file name (eg PTN00012.BIN)
def pattern_name_to_filename(pattern_name):
	x = (ord(pattern_name[0].upper()) - ord('A'))*12
	#print x
	y = int(pattern_name[1:])%PADS_PER_BANK
	#print y
	# pattern_name[0].upper() + ('%07d' % int(pattern_name[1:])) +
	return 'PTN' + str(x + y).zfill(5) + '.BIN'

assert(pattern_name_to_filename("A1")=='PTN00001.BIN')
assert(pattern_name_to_filename("B11")=='PTN00023.BIN')

# parse settings of each pad
def get_pad_info(path):
	# http://sp-forums.com/viewtopic.php?p=60548&sid=840a92a45a7790dd9b593f061ffb4478#p60548
	# http://sp-forums.com/viewtopic.php?p=60553#p60553
	Pad = namedtuple('Pad', 'start end user_start user_end volume lofi loop gate reverse unknown1 channels tempo_mode tempo user_tempo')
	# TODO: sanity check filesize==3840bytes==120pads*32bytes
	f=open(path + PADINFO_PATH, 'rb') # TODO: don't assume user gave sd root path with trailing frontslash
	pads={}
	i=0
	while i<TOTAL_BANKS * PADS_PER_BANK:
		pad_data=f.read(32) # TODO derive 32 from struct format and make the latter a constant
		print(i, binascii.hexlify(pad_data))
		pad = Pad._make(struct.unpack('>IIIIB????BBBII', pad_data))
		print(pad)
		# TODO: sanity check user_start and user_end are even numbers (16bit samples, 2 bytes per sample)

		pads[i+1]=pad
		i+=1
	return pads

# parse pattern
def get_pattern(path, pattern):
	# http://sp-forums.com/viewtopic.php?p=60635&sid=820f29eed0f7275dbeaf776173911736#p60635
	# http://sp-forums.com/viewtopic.php?p=60693&sid=820f29eed0f7275dbeaf776173911736#p60693
	Note = namedtuple('Note', 'delay pad bank_switch unknown2 velocity unknown3 length')
	f=open(path + PATTERN_DIRECTORY + pattern_name_to_filename(pattern),'rb') # TODO: handle command line args w/ argparse
	ptn_filesize = os.fstat(f.fileno()).st_size
	# TODO: sanity check filesize==multiple of BYTES_PER_NOTE
	notes=[]
	i=0
	while i<(ptn_filesize/BYTES_PER_NOTE)-2: # 2*8 trailer bytes at the end of the file
		note_data=f.read(8)
		print(i, binascii.hexlify(note_data))
		note = Note._make(struct.unpack('>BBBBBBH', note_data))
		print("", note)
		notes.append(note)
		
		i+=1

	ptn_trailer=f.read(16)
	ptn_bars = ptn_trailer[9]
	print("ptn_bars", ptn_bars)
	# TODO: sanity check total delay is appropriate for number of bars
	return notes

def notetuple_to_note_filename(note):
	return pad_number_to_filename(notetuple_to_sample_number(note))	

def notetuple_to_sample_number(note):
	if note.bank_switch == 64 or note.bank_switch == 0:
		sample_number = note.pad-46
	elif note.bank_switch == 65 or note.bank_switch == 1:
		sample_number = note.pad-46+PADS_PER_BANK*5
	#elif note.bank_switch == 0:
	#	sample_number = 88 # spacing note
	else:
		print( "unexpected value for bank_switch" )
		sys.exit(1)
		
	return sample_number	

def padtuple_to_trim_samplenums(pad):
	return (pad.user_start-512)/2, (pad.user_end-512)/2

def create_midi_file(pads, notes, midi_tempo, path, pattern):
	midi_file = MIDIFile(numTracks=1)
	midi_file.addTrackName(track=0,time=0,trackName="Roland SP404SX Pattern " + pattern.upper())
	midi_file.addTempo(track=0,time=0,tempo=midi_tempo)

	note_path_to_pitch = {}
	next_available_pitch = 36 # for C1. see "midi note numbers" in http://www.sengpielaudio.com/calculator-notenames.htm
	wave_table_list = []
	path_list = []
	time_in_beats_for_next_note = 0
	for note in notes:
		if note.pad != 128:

			note_filename = notetuple_to_note_filename(note)
			note_path = path + SAMPLE_DIRECTORY + note_filename
			wave_table_list.append(note_filename)
			path_list.append(note_path)
			print("", "note_path:", note_path)
			if note_path not in note_path_to_pitch:
				note_path_to_pitch[note_path] = next_available_pitch
				next_available_pitch += 1
			print("", "pitch:", note_path_to_pitch[note_path])
			if os.path.isfile(note_path):
				pad = pads[notetuple_to_sample_number(note)]
				print("", pad)
				user_start_sample, user_end_sample = padtuple_to_trim_samplenums(pad)
				print("", "user_start_sample:", user_start_sample)
				print("", "user_end_sample:", user_end_sample)
				outfile_path = "/tmp/" + os.path.basename(note_path) # TODO: robust temporary filename selection
				print("", "outfile_path:", outfile_path)
				trim_wav_by_frame_numbers(note_path, outfile_path, user_start_sample, user_end_sample)
				stereo_to_mono(outfile_path, outfile_path + "_mono.wav") # TODO handle stereo samples
				length = note.length / (PPQ * 1.0)
				print("", "length:", length)
				print("", "time:", time_in_beats_for_next_note)
				midi_file.addNote(track=0, channel=0, pitch=note_path_to_pitch[note_path], time=time_in_beats_for_next_note, duration=length, volume=100)
			else:
				print("skipping missing sample")
		else:
			print("skipping empty note")
		delay = note.delay / (PPQ * 1.0)
		print("incrementing time by", delay)
		time_in_beats_for_next_note += delay

	#j = 36
	#while True:
	
	for i in note_path_to_pitch:
		template_wav_path = "template" + ('%02d' % (note_path_to_pitch[i]-35)) + ".wav"
		trimmed_mono_path = "/tmp/" + os.path.basename(i) + "_mono.wav"
		print("pitch:", note_path_to_pitch[i], "-", i, "->", trimmed_mono_path, "->", template_wav_path)
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
	print("out_length_frames", out_length_frames)
	out_file.setparams((in_file.getnchannels(), in_file.getsampwidth(), in_file.getframerate(), out_length_frames, in_file.getcomptype(), in_file.getcompname()))
	in_file.setpos(start_frame)
	out_file.writeframes(in_file.readframes(out_length_frames))

def stereo_to_mono(infile_path, outfile_path):
	sound = AudioSegment.from_wav(infile_path)
	sound = sound.set_channels(1)
	sound.export(outfile_path, format="wav")

def create_template(pattern, wave_table_list, path_list):
	date = datetime.today().strftime('%Y-%m-%d')
	curr_datetime = datetime.today().strftime('%Y-%m-%d-%H:%M:%S')
	instrumentName = "SP-404SX Librarian " + curr_datetime + " PTN_" + pattern.upper()
	begin_key = 36
	end_key = begin_key + len(wave_table_list) - 1
	key_value = begin_key
	wave_table_id = 1
	zones = []
	wavetables = []
	xml_data = ET.Element('sf:pysf')
	xml_data.set('xmlns:sf', '.')
	xml_data.set('xmlns:xsi', 'http://www.w3.org/2001/XMLSchema-instance')
	xml_data.set('version', '3')
	xml_data.set('xsi:schemaLocation', '.')
	sf2 = ET.SubElement(xml_data, 'sf2')
	ET.SubElement(sf2, 'ICRD').text = date
	ifil = ET.SubElement(sf2, 'IFIL')
	major = ET.SubElement(ifil, 'major')
	minor = ET.SubElement(ifil, 'minor')
	major.text = "2"
	minor.text = "1"
	ET.SubElement(sf2, 'INAM').text = "PySF"
	ET.SubElement(sf2, 'IPRD').text = "SBAWE32"
	ET.SubElement(sf2, 'ISFT').text = "PySF"
	ET.SubElement(sf2, 'ISNG').text = "PySF"
	instruments = ET.SubElement(sf2, 'instruments')
	instrument = ET.SubElement(instruments, 'instrument')
	ET.SubElement(instrument, 'id').text = "1"
	ET.SubElement(instrument, 'name').text = instrumentName
	instrument_zones = ET.SubElement(instrument, 'zones')
	presets = ET.SubElement(sf2, 'presets')
	preset = ET.SubElement(presets, 'preset')
	ET.SubElement(preset, 'bank').text = "128"
	ET.SubElement(preset, 'id').text = "1"
	ET.SubElement(preset, 'name').text = instrumentName
	preset_zones = ET.SubElement(preset, 'zones')
	preset_zone = ET.SubElement(preset_zones, 'zone')
	ET.SubElement(preset_zone, 'instrumentId').text = "1"
	key_range = ET.SubElement(preset_zone, 'keyRange')
	ET.SubElement(key_range, 'begin').text = str(begin_key)
	ET.SubElement(key_range, 'end').text = str(end_key)
	wave_tables = ET.SubElement(sf2, 'wavetables')

	for wave_table in wave_table_list:
		wave_table_name = wave_table.replace(".wav","").replace(".aiff","")

		instrument_zone = ET.SubElement(instrument_zones, 'zone')
		instrument_key_range = ET.SubElement(instrument_zone, 'keyRange')
		
		ET.SubElement(instrument_key_range, 'begin').text = str(key_value)
		ET.SubElement(instrument_key_range, 'end').text = str(key_value)
		ET.SubElement(instrument_zone, 'overridingRootKey').text = str(key_value)
		ET.SubElement(instrument_zone, 'sampleModes').text = '0_LoopNone'
		ET.SubElement(instrument_zone, 'wavetableId').text = str(wave_table_id)

		wave_table_data = ET.SubElement(wave_tables, 'wavetable')
		ET.SubElement(wave_table_data, 'file').text = path_list[wave_table_id - 1]
		ET.SubElement(wave_table_data, 'id').text = str(wave_table_id)
		loop = ET.SubElement(wave_table_data, 'loop')
		ET.SubElement(loop, 'begin').text = "1"
		ET.SubElement(loop, 'end').text = "1"
		ET.SubElement(wave_table_data, 'name').text = wave_table_name
		key_value = key_value + 1
		wave_table_id = wave_table_id + 1


	with open("/tmp/pysftemplate.xml", "w") as file:
		file.write("<?xml version=\"1.0\" ?>" + ET.tostring(xml_data).decode("utf-8"))


def create_soundfont_file(pattern):
	pysf.XmlToSf("/tmp/pysftemplate.xml", "PTN_" + pattern.upper() + ".sf2")

def parsepath(path):
	if path[:-1] != "/":
		path = path + "/"
	return path

if __name__ == "__main__":
	'''stereo_to_mono("02.wav", "02m.wav")
	stereo_to_mono("03.wav", "03m.wav")
	stereo_to_mono("04.wav", "04m.wav")
	stereo_to_mono("05.wav", "05m.wav")
	stereo_to_mono("06.wav", "06m.wav")
	stereo_to_mono("07.wav", "07m.wav")
	stereo_to_mono("08.wav", "08m.wav")
	stereo_to_mono("09.wav", "09m.wav")
	stereo_to_mono("10.wav", "10m.wav")
	stereo_to_mono("11.wav", "11m.wav")
	stereo_to_mono("12.wav", "12m.wav")


	sys.exit(1)'''
	midi_tempo = int(sys.argv[3])
	path = parsepath(sys.argv[1])
	pattern = sys.argv[2]
	pads = get_pad_info(path)
	notes = get_pattern(path, pattern)
	wave_table_list, path_list = create_midi_file(pads, notes, midi_tempo, path, pattern)
	create_template(pattern, wave_table_list, path_list)
	create_soundfont_file(pattern)
	





