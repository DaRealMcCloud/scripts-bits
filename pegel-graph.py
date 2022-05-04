# importing the required module
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

# x and y axis values
x = []
y = []
for line in lineListPegel:
    x.append(datetime.strptime(line.split(",")[0]+":"+line.split(",")[1], "%d.%m.%Y:%H:%M") )
    y.append(int(line.split(",")[2]))

plt.xlabel('date')
plt.ylabel('cm')
plt.title('Pegel Prüm!')
ax.plot(x, y)
plt.show()

quit()
