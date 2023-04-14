# Made by Spencer Fairbairn - 2023-04-13

import os
import tempfile
import subprocess
from pydub import AudioSegment

# Required rubberband from https://breakfastquay.com/rubberband/ (included in this project for windows)
# Please make sure it's in your Path so the system can call rubberband from the command line

# Define a function called `nightcore` that takes two arguments, an input file and an output file
def nightcore(input_file, output_file):
    # Convert the input file to WAV format using AudioSegment.from_file method
    song = AudioSegment.from_file(input_file)

    # Create temporary input and output WAV files using NamedTemporaryFile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as input_wav_file, tempfile.NamedTemporaryFile(
            suffix=".wav", delete=False) as output_wav_file:
        # Export the input song to the temporary input WAV file
        song.export(input_wav_file.name, format="wav")

        # Define a command to apply RubberBand for pitch and speed change
        command = f"rubberband -t 0.85 -p 3 \"{input_wav_file.name}\" \"{output_wav_file.name}\""

        # Execute the RubberBand command using subprocess.run
        subprocess.run(command, shell=True, check=True)

        # Load the temporary output WAV file using AudioSegment.from_file method
        output_song = AudioSegment.from_file(output_wav_file.name)

        # Export the output song to the desired output file
        output_song.export(output_file, format="wav")

    # Remove temporary input and output WAV files
    input_wav_file.close()
    os.remove(input_wav_file.name)
    output_wav_file.close()
    os.remove(output_wav_file.name)


# Define input and output file paths
input_file = "input.mp3"
output_file = "nightcore_version.wav"

# Call the nightcore function with the input and output files
nightcore(input_file, output_file)
