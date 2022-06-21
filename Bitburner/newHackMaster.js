/** @param {NS} ns */
export async function main(ns) {
    var target = ns.args[0];

    // start to get half the money.
    var moneyRatio = 0.5

    // time for stuff to happen
    var sleepDelay = 200;

    var hostname = ns.getHostname();
    var ramWeaken = ns.getScriptRam("weaken.js")
	var ramGrow = ns.getScriptRam("grow.js")
	var ramHack = ns.getScriptRam("hack.js")
    
    var maxMemory = ns.getServerMaxRam(hostname)
    maxMemory = maxMemory - ns.getServerUsedRam(hostname);
    var maxThreadsWeaken = Math.floor(maxRam / ramWeaken);
	var maxThreadsGrow = Math.floor(maxRam / ramGrow);
	var maxThreadsHack = Math.floor(maxRam / ramHack);


    while(true){
        ns.sleep(20)
        do{
            ns.sleep(20);
            sleepTimeHack = ns.getHackTime(target);
            sleepTimeGrow = ns.getGrowTime(target);
            sleepTimeWeaken = ns.getWeakenTime(target);

            var player = ns.getPlayer();
            var server = ns.getServer(target);

            var availableMoney = ns.getServerMoneyAvailable(target);

            // calculate needed hack threads to get 'moneyRatio' 
            if (ns.fileExists("Formulas.exe", "home")) {
				hackThreads = Math.ceil(moneyRatio / ns.formulas.hacking.hackPercent(server, player))
			} else {
				hackThreads = Math.ceil(moneyRatio / ns.hackAnalyze(target))
			}

            // calculate needed grow threads to counter hack
			growthFactor = maxMoney / (availableMoney - (availableMoney * moneyRatio));
			if (growthFactor < 1) {
				growthFactor = 1;
				ns.print("Growth factor under 0. MaxMoney:", maxMoney, " , Money: ", availableMoney, " , Money Ratio: ", moneyRatio);
			}

            if (ns.fileExists("Formulas.exe", "home") == true) {
				ns.print("Use formulars.")
				server.moneyAvailable = (availableMoney - (availableMoney * moneyRatio))
				for (let i = 1; i < maxThreadsGrow; i++) {
					var percentGrow = ns.formulas.hacking.growPercent(server, i, player, cores);
					if (percentGrow >= growthFactor) {
						growThreads = i+1;
						break;
					}
					await ns.sleep(20);
				}
			}
			else {
				growThreads = Math.ceil(ns.growthAnalyze(target, growthFactor, cores));
			}
            ns.print("grow factor:", growthFactor, " max money:", maxMoney, "money:", availbleMoney);

            // calculate needed weaken threads to counter hack
            secAfterHack
            threadsWeakenHack=1;
            while(secAfterHack - ns.weakenAnalyze(threadsWeakenHack,cores) > minSecLevel){
                ++threadsWeakenHack;
            }

            // calculate needed weaken threads to counter grow
            secAfterGrow
            threadsWeakenGrow=1;
            while(secAfterGrow - ns.weakenAnalyze(threadsWeakenGrow,cores) > minSecLevel){
                ++threadsWeakenGrow;
            }

            var memoryUsage = (ramHack * hackThreads) + (ramGrow * growThreads) + (ramWeaken * threadsWeakenHack) + (ramWeaken * threadsWeakenGrow)

            if(memoryUsage > maxMemory){
                moneyRatio = moneyRatio*0.95
            } else if (memoryUsage < maxMemory*0.9){
                moneyRatio = moneyRatio*1.05
            } else {
                // sweetspot
            }

            

        }while(memoryUsage > maxMemory)

        // run weaken to finish after hack
        ns.run("weaken.js", threadsWeakenHack, target, "afterHack");
        //wait for delay * 2, for hack to finish
        await ns.sleep(sleepDelay * 2)
        ns.run("weaken.js", threadsWeakenGrow, target, "afterGrow");

        // wait before starting grow
        await ns.sleep(sleepTimeWeaken - sleepTimeGrow - sleepDelay);
        ns.run("grow.js", threadsGrow, target);

        //wait before starting hack
        await ns.sleep(sleepTimeGrow - sleepTimeHack - 2 * sleepDelay);
        ns.run("hack.js", threadsHack, target);

        await ns.sleep(sleepTimeHack + 4 * sleepDelay);

        //wait for weaken to finish
        while( ns.isRunning("weaken.js", hostname, target) == true ){
            ns.sleep(1000)
        }

        
    }

}