import requests
from bs4 import BeautifulSoup
import os.path
import time
from GetWeatherString import GetWeatherString

csvFileName='pegelPruem.csv'
csvFileWeather="weatherPruem.csv"
url = 'https://www.hochwasser-rlp.de/karte/einzelpegel/flussgebiet/mosel/pegel/PRUEM_2/darstellung/tabellarisch'

while True:
    print("Get data from the web.")
    html_text = requests.get(url).text
    soup = BeautifulSoup(html_text, 'html.parser')

    print("Parse data.")
    lineNumber = lineStart = 0 
    part=0
    dataEntry=""
    dataList=[]
    for line in soup.get_text().splitlines():
        lineNumber = lineNumber+1

        if lineStart != 0:
            if line != "":
                #print(line, part, lineNumber)
                part=part+1

                if part == 1 :
                    #print("set")
                    dataEntry=line
                elif part < 4:
                    #print("add")
                    dataEntry=dataEntry+","+line
                else:
                    dataList.append(dataEntry)
                    part=1
                    dataEntry=line 
        else:
            if line == "Wasserstand in cm":
                lineStart = lineNumber
        
    print("Data gathered")
    dataList.reverse()
    #print(dataList)


    if os.path.isfile(csvFileName):
        print("Append to existing file")
        with open(csvFileName) as f:
            existingLines=f.read().splitlines()
            #print(existingLines)
            #quit()

        for dataPoint in dataList:
            existingLineFound=False
            for existingLine in existingLines:
                #print("check" + existingLine)
                if existingLine == dataPoint:
                    existingLineFound=True

            if existingLineFound == False:
                print("adding :" + dataPoint)
                with open(csvFileName,'a') as f:
                    f.write(dataPoint+"\n")
    else:
        print("File does not exist.Creating")
        with open('pegelPruem.csv',"x") as f:
            for dataPoint in dataList:
                f.write(dataPoint+"\n")

    weatherString=GetWeatherString("Prüm","DE")
    with open(csvFileWeather,'a') as f:
        f.write(weatherString+"\n")
    
    time.sleep(900)