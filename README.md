# MakeNightcore
is a command-line Python program that converts an input audio file to a Nightcore version by increasing the pitch and speed of the original audio. It uses the PyDub library for audio processing and RubberBand for pitch and speed change.

#Prerequisites
Before you begin, ensure you have the following dependencies installed:

Python 3.6 or higher
RubberBand Audio Time Stretcher
ffmpeg (required by PyDub)
Install the required Python libraries:

```pip install pydub```
Usage
To use the Nightcore Creator, simply run the MakeNightcore.py script with the -s or --source flag followed by the path to the input audio file:


```python MakeNightcore.py -s input.mp3```
By default, the output file will be saved as nightcore_version.wav in the current directory. You can specify a different output file using the -o or --output flag:


```python MakeNightcore.py -s input.mp3 -o output.wav```
Example
Input: input.mp3
Output: nightcore_version.wav

```python MakeNightcore.py -s input.mp3```
