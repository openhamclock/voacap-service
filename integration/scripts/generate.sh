#!/usr/bin/env bash
echo processing standards to build integrated code under voacap-service license model
HERE="$(realpath -s "$(dirname "$0")")"
intscripts="$(realpath -s $HERE)"
intbuild="$(realpath -s $HERE/../build)"
intstandards="$(realpath -s $HERE/../standards)"
intapp="$(realpath -s $HERE/../../app)"
echo integration and testing should be done on files included in $intstandards in standards repo before adding to the copies in this repo 
echo generating code
$intscripts/insert_license.bash $intstandards/voacap.ant.csv   $intscripts/license_csv.txt  $intbuild/voacap.ant.csv
$intscripts/insert_license.bash $intstandards/gen_antenna_data.py   $intscripts/license.agpl.kr8x.txt  $intbuild/gen_antenna_data.py
$intscripts/insert_license.bash $intstandards/antenna_lookup.py   $intscripts/license.agpl.kr8x.txt  $intbuild/antenna_lookup.py
echo generating voacap antenna_data.py and setting execute permissions
chmod +x  $intbuild/*py
python3  $intbuild/gen_antenna_data.py  $intbuild/voacap.ant.csv  $intbuild/antenna_data.py
echo copying generated scripts to delivery app folder
echo and removing execute permission from files
chmod ogu-x $intbuild/antenna_data.py
chmod ogu-x $intbuild/antenna_lookup.py
cp $intbuild/antenna_data.py $intapp/antenna_data.py
cp $intbuild/antenna_lookup.py $intapp/antenna_lookup.py

