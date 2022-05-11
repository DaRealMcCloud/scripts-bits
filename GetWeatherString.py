import requests
import json

def GetWeatherString(cityName,countryCode):
    limit=1
    apiKeyFile="apiKeyOpenweathermap.txt"
    with open(apiKeyFile) as f:
        apiKey=f.read().strip()

    url=f"http://api.openweathermap.org/geo/1.0/direct?q={cityName},{countryCode}&limit={limit}&appid={apiKey}"
    response = requests.get(url).text
    response=response.rstrip(']')
    response=response.lstrip('[')
    city=json.loads(response)
   
    units="metric"
    lat=city["lat"]
    lon=city["lon"]
    url = f'https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&units={units}&appid={apiKey}'
    response = requests.get(url).text


    print("Get data from the web.")
    response = requests.get(url).text
    weather=json.loads(response)

    #print("Got this:")
    #print(weather)
        
    weatherString = weather["name"]+",Temp:"+str(weather["main"]["temp"])+",Humidity:"+str(weather["main"]["humidity"])+",Pressure:"+str(weather["main"]["grnd_level"])
    try:
        weatherString=weatherString+",Rain1h:"+str(weather["rain"]["1h"])
    except:
        print("No rain in the last hour")

    return weatherString


#GetWeatherString("Solva","GB")