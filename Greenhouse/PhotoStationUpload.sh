curl -X POST --data 'api=SYNO.API.Auth&version=3&method=login&account=<USER>&passwd=<PASSWORD>' https://10.0.0.10/photo/webapi/auth.cgi

{
   "data":{
      "did":"3ezTHmf9ekMNjM-nF8J3qI5VzsMXlJBjRcNHotIwzKTc3ju2YGB7y9IIvOPxHTSuvvZmVOxM7u7f57w7ndtmog",
      "sid":"k76NZ-C7inEyhENeX7Jfu7yB28Wk_wbWEszF2ZtHSB8dcW68WQYQbTvF0Oqo21zoQuitKmoFop-kSvJQGHG1oU"
   },
   "success":true
}


curl -b PHPSESSID=xxx \
     -F 'original=@/path/to/original/file' \
     -F 'thumb_small=@/path/to/small/thumb' \
     -F 'thumb_large=@/path/to/large/thumb' \
     'http://10.0.0.10/photo/webapi/file.php?api=SYNO.PhotoStation.File&method=uploadphoto&version=1&dest_folder_path=Substanzen/Growroom/Timelapse/Schwede&duplicate=ignore&filename=myfilename.jpg&mtime=1579384308'



curl 'http://<IP_ADDRESS>/photo/webapi/auth.cgi?api=SYNO.API.Auth&version=3&method=logout'




