## Introduction

These scripts are provided to generate new versions of backend server files based on standards from
https://github.com/openhamclock/hamclock-standards/tree/main/proposals/PROP-001/antennas

The generate script only needs to be run once for a revision of the standard, when a new implementation is done, or
when an error is detected that requires an antenna index to be removed from supported antenna types.


The generate script generates and places the following files in /app

> antenna_data.py

> antenna_lookup.py

Currently the standard uses the same folder/file format as contained in voicapl library itshfbc/antennas.
In the future this one-to-one mapping may change and require new design and testing.

All files in folder build are generated and do not need to be added to the repo.
