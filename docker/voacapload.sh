#!/bin/bash
load=${1:-100}
echo $load>load.txt

vserv=voacap-service
vs=/app

vf1=allowarea_TOA
vf2=allowarea_MUF
vf3=allowarea_REL

echo pushing voacap updates
docker cp load.txt $vserv:$vs/$vf1
docker exec $vserv bash -c "chown root:root $vs/$vf1"
docker exec $vserv bash -c "chmod ugo+r $vs/$vf1"
docker cp load.txt $vserv:$vs/$vf2
docker exec $vserv bash -c "chown root:root $vs/$vf2"
docker exec $vserv bash -c "chmod ugo+r $vs/$vf2"
docker cp load.txt $vserv:$vs/$vf3
docker exec $vserv bash -c "chown root:root $vs/$vf3"
docker exec $vserv bash -c "chmod ugo+r $vs/$vf3"
