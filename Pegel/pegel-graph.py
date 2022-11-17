import matplotlib.pyplot as plt
from datetime import datetime

csvPegelFileName='pegelPruem.csv'
with open(csvPegelFileName) as f:
    lineListPegel=f.read().splitlines()

csvWeatherFileName='weatherPruem.csv'
with open(csvWeatherFileName) as f:
    lineListWeather=f.read().splitlines()


fig, ax = plt.subplots()
fig.subplots_adjust(right=0.75)

twin1 = ax.twinx()
twin2 = ax.twinx()

# Offset the right spine of twin2.  The ticks and label have already been
# placed on the right by twinx above.
twin2.spines.right.set_position(("axes", 1.2))

x = []
y = []
for line in lineListPegel:
    x.append(datetime.strptime(line.split(",")[0]+":"+line.split(",")[1], "%d.%m.%Y:%H:%M") )
    y.append(int(line.split(",")[2]))
p1, = ax.plot(x, y, "b-", label="Pegel")

x2 = []
y2 = []
for line in lineListWeather:
    x2.append(datetime.strptime(line.split(",")[0], "%d.%m.%Y:%H:%M") )
    y2.append(float(line.split(",")[2].split(':')[1]))
p2, = twin1.plot(x2, y2, "r-", label="Temperature")

x3 = []
y3 = []
for line in lineListWeather:
    x3.append(datetime.strptime(line.split(",")[0], "%d.%m.%Y:%H:%M") )
    y3.append(float(line.split(",")[3].split(':')[1]))
#p3, = twin2.plot(x3, y3, "g-", label="Humidity")

#ax.set_xlim(0, 2)
ax.set_ylim(0, 100)
twin1.set_ylim(-10, 40)
twin2.set_ylim(0, 100)

ax.set_xlabel("Date")
ax.set_ylabel("Pegel")
twin1.set_ylabel("Temperature")
twin2.set_ylabel("Humidity")

ax.yaxis.label.set_color(p1.get_color())
twin1.yaxis.label.set_color(p2.get_color())
#twin2.yaxis.label.set_color(p3.get_color())

tkw = dict(size=4, width=1.5)
ax.tick_params(axis='y', colors=p1.get_color(), **tkw)
twin1.tick_params(axis='y', colors=p2.get_color(), **tkw)
#twin2.tick_params(axis='y', colors=p3.get_color(), **tkw)
ax.tick_params(axis='x', **tkw)

ax.legend(handles=[p1, p2])#, p3])

plt.show()
