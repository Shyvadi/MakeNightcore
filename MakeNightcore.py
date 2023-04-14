import os
import tempfile
import subprocess
import argparse
from pydub import AudioSegment


def nightcore(input_file, output_file):
    song = AudioSegment.from_file(input_file)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as input_wav_file, tempfile.NamedTemporaryFile(
            suffix=".wav", delete=False) as output_wav_file:
        song.export(input_wav_file.name, format="wav")

        command = f"rubberband -t 0.85 -p 3 \"{input_wav_file.name}\" \"{output_wav_file.name}\""
        subprocess.run(command, shell=True, check=True)

        output_song = AudioSegment.from_file(output_wav_file.name)
        output_song.export(output_file, format="wav")

    input_wav_file.close()
    os.remove(input_wav_file.name)
    output_wav_file.close()
    os.remove(output_wav_file.name)


def main():
    parser = argparse.ArgumentParser(description="Create a nightcore version of an input audio file.")
    parser.add_argument("-s", "--source", required=True, help="Path to the input audio file")
    parser.add_argument("-o", "--output", default="nightcore_version.wav", help="Path to the output audio file")

    args = parser.parse_args()

    input_file = args.source
    output_file = args.output

    nightcore(input_file, output_file)


if __name__ == "__main__":
    main()
