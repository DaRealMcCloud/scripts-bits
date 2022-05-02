# importing the required module
import matplotlib.pyplot as plt
csvFileName='pegelPruem.csv'

with open(csvFileName) as f:
    lineList=f.read().splitlines()

# x axis values
x = []
# corresponding y axis values
y = []

for line in lineList:
    x.append(line.split(",")[0]+":"+line.split(",")[1])
    y.append(line.split(",")[2])

 
# plotting the points
plt.plot(x, y)
 
# naming the x axis
plt.xlabel('cm')
# naming the y axis
plt.ylabel('date')
 
# giving a title to my graph
plt.title('Pegel Prüm!')
 
# function to show the plot
plt.show()