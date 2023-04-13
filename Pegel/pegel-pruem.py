# coding=utf-8
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import os.path
import time
from GetWeatherString import GetWeatherString

csvFileName='pegelPruem.csv'
csvFileWeather="weatherPruem.csv"
url = 'https://www.hochwasser-rlp.de/karte/einzelpegel/flussgebiet/mosel/pegel/PRUEM_2/darstellung/tabellarisch'

while True:
	try:
		print("Get data from the web.")
		try:
			html_text = requests.get(url).text
		except:
			print("Error happened while getting data from web.")
			time.sleep(60)
		else:
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
			print(len(dataList))
	
	
			if os.path.isfile(csvFileName):
				if len(dataList) > 0:
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
			today=datetime.now()
			with open(csvFileWeather,'a') as f:
				f.write(today.strftime("%d.%m.%Y:%H:%M")+','+weatherString+"\n")
			
			time.sleep(900)
	except KeyboardInterrupt:
		print("Quitting")
		quit()
	except Exception as e:
		print("Error happened in the script")
		print(e)
	
