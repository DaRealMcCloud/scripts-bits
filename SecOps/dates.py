
datesFile="dates.lst"
lineCount=0

with open(datesFile,'a') as f:
    for year in range(1900,2023):
        for month in range(1,13):
            if month < 10:
                month= '0' + str(month)
            for day in range(1,32):
                if day < 10:
                    day='0'+str(day)

                #print( str(day) + str(month) + str(year)) 
                f.write(str(day) + str(month) + str(year) + "\n")
                lineCount+=1

print("Generated " + str(lineCount) + " lines.")