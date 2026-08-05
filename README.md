# BFG Resplash
A fork of a certain server to make it more user friendly to locally host and include some missing stuff from 1.2.9, as well being user friendly regarding in-game DLC updates.

## How do you host it?
You will need at least python 3.13. A batch script to install the requirements is included for convenience.
Once you have the requirements in place

## How do I edit an MSM Big Fish executable to work with this stuff?
Use something like HxD and replace the following bytes:
"68 74 74 70 73 3A 2F 2F 62 66 2D 61 75 74 68 2E 62 62 62 67 61 6D 65 2E 6E 65 74 2F"
with:
"68 74 74 70 3A 2F 2F 31 32 37 2E 30 2E 30 2E 31 3A 39 30 30 2F 00 00 00 00 00 00 00"

## How do I make it NOT ugly and retro looking???
In the "files" folder there's an example mod folder. Rename the original *1.2.9* folder to something else and rename the example folder to *1.2.9* instead.

## Some credits!
Somebody on Discord helped me fix the scripts to include them here.
Somebody else helped me with some other misc. data regarding missing features.

Now go on and "have fun my friend"
