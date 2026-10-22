#!/usr/bin/env bash
# Family Relay voice box: a g7 GPU spot (AWS dev2, eu-south-2) running our VoxCPM2 TTS server (+ Qwen3-ASR for checks).
# Local side uses only aws CLI / ssh / scp / tar. Models and venvs come from the shared S3 assets of the voxcpm2 skill.
#
#   bash deploy/voice_gpu.sh up        start the spot instance (tag Project=family-relay-voice)  - cost starts
#   bash deploy/voice_gpu.sh setup     upload skill scripts, restore models/venvs from S3, start the VoxCPM2 server
#   bash deploy/voice_gpu.sh clips     family voices for the scenario + assistant reference voice + latency bench
#   bash deploy/voice_gpu.sh pull      clips -> simulator/static/clips, assistant ref -> assets/voice, bench -> tmp
#   bash deploy/voice_gpu.sh stt       start the Qwen3-ASR STT server on :8002 (target of FR_STT_ENGINE=http)
#   bash deploy/voice_gpu.sh tunnel    forward TTS :8000 and STT :8002 to localhost:18000 / 18002 (keep running while demoing)
#   bash deploy/voice_gpu.sh down      terminate and confirm (spot request state shown)
#   bash deploy/voice_gpu.sh status
set -euo pipefail
export AWS_PAGER="" MSYS_NO_PATHCONV=1
HERE=$(cd "$(dirname "$0")" && pwd); APP=$(cd "$HERE/.." && pwd)
SKILL="$HOME/.claude/skills/voxcpm2"
PROFILE=${AWS_PROFILE:-default}; REGION=${REGION:-eu-south-2}; PROJECT=family-relay-voice
TYPES=(g7.2xlarge); MAX_SPOT=0.60
# your account: subnets to try (space separated) and their VPC, a security group with port 22, an instance profile that
# can read the voxcpm2 assets (VOX_S3), an EC2 key pair and its pem
read -r -a SUBNETS <<< "${VOICE_SUBNETS:?set VOICE_SUBNETS}"; VPC_ID=${VOICE_VPC_ID:?set VOICE_VPC_ID}
SG_NAME=${VOICE_SG_NAME:?set VOICE_SG_NAME}; IAM_PROFILE=${VOICE_IAM_PROFILE:?set VOICE_IAM_PROFILE}
KEY_NAME=${VOICE_KEY_NAME:?set VOICE_KEY_NAME}; KEY=${VOICE_KEY_PEM:-$HOME/.ssh/$KEY_NAME.pem}
AMI_PATTERN="Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 24.04)*"; ROOT_GB=75
PULL_TMP=${PULL_TMP:-$APP/../../../../tmp/fr-voice-pull}
A() { aws "$@" --profile $PROFILE --region $REGION --output text; }
ip() { A ec2 describe-instances --filters "Name=tag:Project,Values=$PROJECT" Name=instance-state-name,Values=running \
        --query "Reservations[0].Instances[0].PublicIpAddress" | grep -v None || true; }
SSHO=(-i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$HOME/.ssh/known_hosts_fr" -o ServerAliveInterval=30 -o ConnectTimeout=10)
R() { local h; h=$(ip); [ -n "$h" ] || { echo "nothing running"; exit 1; }; ssh "${SSHO[@]}" ubuntu@"$h" "$@"; }
CP() { local h; h=$(ip); scp -q "${SSHO[@]}" "${@/#:/ubuntu@$h:}"; }

up() {
    if [ -n "$(ip)" ]; then echo "already up: $(ip)"; return; fi
    local sg ami root
    sg=$(A ec2 describe-security-groups --filters Name=group-name,Values=$SG_NAME Name=vpc-id,Values=$VPC_ID --query "SecurityGroups[0].GroupId")
    ami=$(A ec2 describe-images --owners amazon --filters "Name=name,Values=$AMI_PATTERN" Name=state,Values=available --query "sort_by(Images,&CreationDate)[-1].ImageId")
    root=$(A ec2 describe-images --image-ids "$ami" --query "Images[0].RootDeviceName")
    local tags="ResourceType=instance,Tags=[{Key=Name,Value=$PROJECT},{Key=Project,Value=$PROJECT}]"
    for t in "${TYPES[@]}"; do for s in "${SUBNETS[@]}"; do
        local az p out
        az=$(A ec2 describe-subnets --subnet-ids "$s" --query "Subnets[0].AvailabilityZone")
        p=$(A ec2 describe-spot-price-history --instance-types "$t" --product-descriptions Linux/UNIX --availability-zone "$az" --query "SpotPriceHistory[0].SpotPrice")
        { [ "$p" = None ] || [ -z "$p" ]; } && continue
        awk "BEGIN{exit !($p <= $MAX_SPOT)}" || { echo "  $t $az \$$p over cap"; continue; }
        echo "  $t $az \$$p/h ..."
        if out=$(A ec2 run-instances --image-id "$ami" --instance-type "$t" --key-name $KEY_NAME --security-group-ids "$sg" \
                --subnet-id "$s" --associate-public-ip-address --iam-instance-profile Name=$IAM_PROFILE --instance-market-options MarketType=spot \
                --block-device-mappings "DeviceName=$root,Ebs={VolumeSize=$ROOT_GB,VolumeType=gp3,DeleteOnTermination=true}" \
                --tag-specifications "$tags" "${tags/instance/volume}" --count 1 --query "Instances[0].InstanceId" 2>&1); then
            A ec2 wait instance-running --instance-ids "$out"; echo "up: $out $t $az $(ip)"
            for _ in $(seq 60); do R true 2>/dev/null && return; sleep 5; done; echo "ssh not reachable"; exit 1
        fi
        echo "$out" | grep -qE "InsufficientInstanceCapacity|InsufficientHostCapacity|Unsupported|MaxSpotInstanceCountExceeded" || { echo "$out"; exit 1; }
        echo "    no capacity"
    done; done
    echo "no capacity anywhere - try later"; exit 1
}

setup() {
    R "mkdir -p ~/voxcpm2 ~/fr-voice"
    CP "$SKILL"/remote/* :voxcpm2/
    CP "$HERE"/voice_remote/make_clips.py "$APP"/simulator/static/scenario.json :fr-voice/
    R "chmod +x ~/voxcpm2/*.sh && ~/voxcpm2/setup.sh"
}

clips() {
    R "set -e; ~/voxcpm2/serve.sh status | grep -q up || ~/voxcpm2/serve.sh start
       export HF_HUB_OFFLINE=1; rm -rf /nvme/fr-voice; mkdir -p /nvme/fr-voice
       /nvme/voxcpm2/venv-asr/bin/python ~/fr-voice/make_clips.py ~/fr-voice/scenario.json /nvme/fr-voice 2>&1 | grep -v Warning
       tar czf ~/fr-voice/out.tgz -C /nvme/fr-voice ."
}

pull() {
    rm -rf "$PULL_TMP"; mkdir -p "$PULL_TMP"
    CP :fr-voice/out.tgz "$PULL_TMP/"
    tar xzf "$PULL_TMP/out.tgz" -C "$PULL_TMP"
    rm -rf "$APP/simulator/static/clips"; cp -r "$PULL_TMP/clips" "$APP/simulator/static/clips"
    mkdir -p "$APP/assets/voice"; cp "$PULL_TMP"/assistant_ref.wav "$PULL_TMP"/assistant_ref.txt "$APP/assets/voice/"
    echo "clips -> $APP/simulator/static/clips, assistant ref -> $APP/assets/voice, bench -> $PULL_TMP/bench.json"
}

stt() {
    CP "$HERE"/voice_remote/stt_server.py :fr-voice/
    R 'command -v ffmpeg >/dev/null || (sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg >/dev/null)
       cd ~/fr-voice; if [ -f stt.pid ] && kill -0 $(cat stt.pid) 2>/dev/null; then kill $(cat stt.pid); sleep 2; fi
       HF_HUB_OFFLINE=1 nohup /nvme/voxcpm2/venv-asr/bin/python stt_server.py > stt.log 2>&1 & echo $! > stt.pid
       for i in $(seq 60); do curl -s -o /dev/null -X POST 127.0.0.1:8002/stt && break; sleep 2; done; tail -1 stt.log'
}

tunnel() { local h; h=$(ip); exec ssh "${SSHO[@]}" -N -L 18000:127.0.0.1:8000 -L 18002:127.0.0.1:8002 ubuntu@"$h"; }

down() {
    local ids; ids=$(A ec2 describe-instances --filters "Name=tag:Project,Values=$PROJECT" Name=instance-state-name,Values=pending,running,stopping,stopped --query "Reservations[].Instances[].InstanceId")
    [ -z "$ids" ] && { echo "nothing running"; return; }
    A ec2 terminate-instances --instance-ids $ids >/dev/null
    A ec2 wait instance-terminated --instance-ids $ids
    echo "terminated: $ids"
    A ec2 describe-spot-instance-requests --filters "Name=instance-id,Values=${ids// /,}" --query "SpotInstanceRequests[].[SpotInstanceRequestId,State,Status.Code]"
}

status() { A ec2 describe-instances --filters "Name=tag:Project,Values=$PROJECT" Name=instance-state-name,Values=pending,running \
           --query "Reservations[].Instances[].[InstanceId,InstanceType,PublicIpAddress,LaunchTime]" | grep . || echo "nothing running"; }

case "${1:-}" in
    up) up ;; setup) setup ;; clips) clips ;; stt) stt ;; pull) pull ;; tunnel) tunnel ;; down) down ;; status) status ;;
    *) sed -n '2,12p' "$0"; exit 1 ;;
esac
