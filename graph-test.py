# importing the required module
from tokenize import Double
from unicodedata import decimal
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
from datetime import datetime

csvPegelFileName='pegelPruem.csv'
with open(csvPegelFileName) as f:
    lineListPegel=f.read().splitlines()

csvWeatherFileName='weatherPruem.csv'
with open(csvWeatherFileName) as f:
    lineListWeather=f.read().splitlines()

fig, ax = plt.subplots()  # Create a figure containing a single axes
ax.set_xlabel('Datum') 


ax2 = ax.twinx()




ax.set_ylabel('Pegel in cm') 
x = []
y = []
for line in lineListPegel:
    x.append(datetime.strptime(line.split(",")[0]+":"+line.split(",")[1], "%d.%m.%Y:%H:%M") )
    y.append(int(line.split(",")[2]))
ax.plot(x, y, color='blue')

ax2.set_ylabel("Temperatur in C")
x2 = []
y2 = []
for line in lineListWeather:
    x2.append(datetime.strptime(line.split(",")[0], "%d.%m.%Y:%H:%M") )
    y2.append(float(line.split(",")[2].split(':')[1].split(' ')[0]))
ax2.plot(x2, y2, color='red')

#Hummidity in %
#Air pressure grnd level in hPa

plt.title('Pegel Prüm! in cm')
plt.show()

quit()
