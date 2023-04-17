#!/bin/bash

# Set the NFS share address, mount point and options
NFS_SHARE_ADDRESS="nas-cloud-01.fritz.box:/photo/Substanzen/Growroom/Timelapse"
NFS_MOUNT_POINT="/mnt/timelapse"
NFS_MOUNT_OPTIONS="nfsvers=4.1,rsize=8192,wsize=8192,timeo=14,intr"

# Mount the NFS share if it's not already mounted
if ! mountpoint -q "$NFS_MOUNT_POINT"; then
  mkdir -p "$NFS_MOUNT_POINT"
  mount -t nfs "$NFS_SHARE_ADDRESS" "$NFS_MOUNT_POINT" -o "$NFS_MOUNT_OPTIONS"
fi

# Set the directory to store the images and videos
IMAGES_DIR="$NFS_MOUNT_POINT/images"
DAILY_VIDEOS_DIR="$NFS_MOUNT_POINT/daily_videos"
WEEKLY_VIDEOS_DIR="$NFS_MOUNT_POINT/weekly_videos"
LIGHTS_ON=5
LIGHTS_OFF=20

# Create directories if they don't exist
mkdir -p "$IMAGES_DIR"
mkdir -p "$DAILY_VIDEOS_DIR"
mkdir -p "$WEEKLY_VIDEOS_DIR"

while true; do
  # Get the current hour
  HOUR=$(date +%H)

  # Check if the current time is between 8am and 8pm
  if [ $HOUR -ge $LIGHTS_ON ] && [ $HOUR -lt $LIGHTS_OFF ]; then
    # Take a picture using fswebcam and save it with a timestamp
    TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
    fswebcam -q -r 1920*1080 "$IMAGES_DIR/$TIMESTAMP.jpg"

    # Wait for 1 minute before taking the next picture
    sleep 60
  else
    # Wait for 1 minute before checking the time again
    sleep 60
  fi

  # Check if it's a new day
  if [ "$(date +%H%M)" == "0000" ]; then
    # Combine the pictures into a video using ffmpeg
    VIDEO_FILENAME=$(date +%Y-%m-%d).mp4
    ffmpeg -framerate 1/60 -pattern_type glob -i "$IMAGES_DIR/*.jpg" -c:v libx264 -r 60 -pix_fmt yuv420p "$DAILY_VIDEOS_DIR/$VIDEO_FILENAME"
    #ffmpeg -framerate 1/60 -pattern_type glob -i "/mnt/timelapse/images/*.jpg" -c:v libx264 -r 60 -pix_fmt yuv420p "/mnt/timelapse/daily_videos/testVid.mp4"

    # Remove the images from the previous day
    rm -f "$IMAGES_DIR/*.jpg"

    # Check if it's a new week (i.e. Sunday)
    if [ "$(date +%u)" == "7" ]; then
      # Combine the daily videos into a weekly video using ffmpeg
      WEEKLY_VIDEO_FILENAME=$(date +%Y-%m-%d)_to_$(date -d 'last sunday' +%Y-%m-%d).mp4
      ffmpeg -f concat -safe 0 -i <(find "$DAILY_VIDEOS_DIR" -type f -name '*.mp4' | sort) -c copy "$WEEKLY_VIDEOS_DIR/$WEEKLY_VIDEO_FILENAME"

      # Remove the daily videos from the previous week
      rm -f "$DAILY_VIDEOS_DIR/*.mp4"
    fi
  fi
done
