#!/bin/bash


# Check if the script was called with exactly one parameter
if [ $# -ne 3 ]; then
  echo "Error: Incorrect number of parameters. Please provide exactly three parameter. Name, Sunrise, Sunset"
  exit 1
fi

# Store the parameters in a variable
ENVIRONMENT_NAME="$1"
SUNRISE="$2"
SUNSET="$3"
RENDERTIME="00"

if [ $SUNRISE -lt 0 ] || [ $SUNSET -lt 0 ] || [ $SUNRISE -gt 23 ] || [ $SUNSET -gt 23 ]; then
  echo "Invalid times"
  exit -1
fi

if [ $SUNSET -eq 23 ]; then
  echo "Timelapse creation will be attempted at midnight."
else
  $RENDERTIME = $SUNSET+1
  echo "Timelapse creation will be attempted at " $RENDERTIME
fi

# Set the NFS share address, mount point and options
NFS_SHARE_ADDRESS="nas-cloud-01.fritz.box:/volume1/photo/Substanzen/Growroom/Timelapse"
NFS_MOUNT_POINT="/mnt/timelapse"
NFS_MOUNT_OPTIONS="nfsvers=4.1,rsize=8192,wsize=8192,timeo=14,intr"

# Mount the NFS share if it's not already mounted
if ! mountpoint -q "$NFS_MOUNT_POINT"; then
  mkdir -p "$NFS_MOUNT_POINT"
  mount -t nfs "$NFS_SHARE_ADDRESS" "$NFS_MOUNT_POINT" #-o "$NFS_MOUNT_OPTIONS"
fi

DATE=$(date +%Y-%m-%d)
HOUR=$(date +%H)
# Set the directory to store the images and videos
$IMAGES_DIR="$NFS_MOUNT_POINT/$ENVIRONMENT_NAME/$DATE/images"
$VIDEOS_DIR="$NFS_MOUNT_POINT/$ENVIRONMENT_NAME/$DATE"
$WEEKLY_VIDEOS_DIR="$NFS_MOUNT_POINT/$ENVIRONMENT_NAME/full"


# Create directories if they don't exist
mkdir -p "$IMAGES_DIR"
#mkdir -p "$DAILY_VIDEOS_DIR"
mkdir -p "$WEEKLY_VIDEOS_DIR"

# Get Todays date
TODAY=$DATE
declare -a LAST_SEVEN_DAYS=()
DAY=1

while true; do
  # Get the current hour
  DATE=$(date +%Y-%m-%d)
  HOUR=$(date +%H)

  if [ "$DATE" != "$TODAY" ]; then
    $TODAY = $DATE
    $IMAGES_DIR="$NFS_MOUNT_POINT/$ENVIRONMENT_NAME/$DATE/images"
    $VIDEOS_DIR="$NFS_MOUNT_POINT/$ENVIRONMENT_NAME/$DATE"
    mkdir -p "$IMAGES_DIR"
    unset LAST_SEVEN_DAYS
    $DAY= 1
    while [ $DAY -lt 7 ]; do
      PAST_DAY= $(date --date="$DAY day ago")
      if [ -f "$NFS_MOUNT_POINT/$ENVIRONMENT_NAME/$PAST_DAY/daily.mp4" ]; then
        echo "$NFS_MOUNT_POINT/$ENVIRONMENT_NAME/$PAST_DAY/daily.mp4 exists."
        $LAST_SEVEN_DAYS+="FILE $NFS_MOUNT_POINT/$ENVIRONMENT_NAME/$PAST_DAY/daily.mp4\\n"
      fi
      $DAY=$DAY+1
    done
  fi

  # Check if the current time is between Sunrise and Sunset
  if [ $HOUR -gt $SUNRISE ] && [ $HOUR -lt $SUNSET ]; then
    # Take a picture using fswebcam and save it with a timestamp
    TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
    (
    if [ $ENVIRONMENT_NAME == "Schwede" ]; then
     #Logitech BRIO UHD Pro Business
     fswebcam -d /dev/video2 -q --skip 200 --delay 5 -r 2560x1440 --flip h,v --jpeg 95 --no-banner --set "White Balance Temperature, Auto"=False --set "White Balance Temperature"=3900 --set "LED1 Mode"=Off --set "Exposure, Auto Priority"=False --set "Exposure (Absolute)"=10 --set "Backlight Compensation"=0 --set Gain=0 --set "Zoom, Absolute"=100 --set "Exposure, Auto"="Manual Mode" "$IMAGES_DIR/$TIMESTAMP.jpg"
    elif [ $ENVIRONMENT_NAME == "Veggie" ]; then
     #Logitech C920 Pro
     fswebcam -d /dev/video0 -q --skip 200 --delay 5 -r 1920x1080 --flip h,v --jpeg 95 --no-banner --set "White Balance Temperature"=6500 --set Brightness=128 --set "White Balance Temperature, Auto"=False --set "Exposure, Auto Priority"=False --set "Exposure (Absolute)"=10 --set --set Gain 0 "Exposure, Auto"="Manual Mode" "$IMAGES_DIR/$TIMESTAMP.jpg"
    else
     fswebcam
    fi
    )
    echo "Catured image " $TIMESTAMP
    # Wait for 1 minute before taking the next picture
    sleep 900
  else
    # Wait for 1 minute before checking the time again
    sleep 50
  fi

  # Check if it's a new day
  if [ "$(date +%H)" == "$RENDERTIME" ]; then
    while [ -f "$NFS_MOUNT_POINT/semaphore.txt" ]
    do
      if [ -f "$NFS_MOUNT_POINT/semaphore.txt"]; then
        # IF render is already in progress wait 60 seconds and check again.
        sleep 60
      else
        touch "$NFS_MOUNT_POINT/semaphore.txt"
        VIDEO_FILENAME=daily.mp4
        echo "Create Video " $VIDEO_FILENAME
        ffmpeg -pattern_type glob -i "$IMAGES_DIR/*.jpg" -c:v libx265 -r 40 "$VIDEOS_DIR/$VIDEO_FILENAME"
        #ffmpeg -pattern_type glob -i "/mnt/timelapse/images/*.jpg" -c:v libx264 -r 40 "/mnt/timelapse/daily_videos/testVideo1.mp4"
        echo "done"
        ffmpeg -f concat -safe 0 -i "$LAST_SEVEN_DAYS" -c copy "$WEEKLY_VIDEOS_DIR"
        rm "$NFS_MOUNT_POINT/semaphore.txt"
        # Send to photoStation
        # curl -X POST --data 'api=SYNO.API.Auth&version=3&method=login&account=<USER>&passwd=<PASSWORD>' https://10.0.0.10/photo/webapi/auth.cgi
        # curl 'http://10.0.0.10/photo/webapi/auth.cgi?api=SYNO.API.Auth&version=3&method=logout'
      fi
    done
    # Remove the images from the previous day
    #rm -f "$IMAGES_DIR/*.jpg"

    # wait one hour
    sleep 3600

  fi
done
