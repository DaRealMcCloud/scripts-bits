/** @param {NS} ns */
export async function main(ns) {
	ns.disableLog("ALL")
	var target = ns.args[0];
	var iteration = ns.args[1];

	var hostname = ns.getHostname()
	var cores = ns.getServer(hostname).cpuCores;

	var moneyRatio = 0.35
	var growthRatio = 1 / (1 - moneyRatio)

	var sleepDelay = 200;

	// caculate how much ram each script takes
	var ramWeaken = ns.getScriptRam("weaken.js")
	var weakenTimeMs = ns.getWeakenTime(target);
	var threadsWeaken = 1;
	var ramGrow = ns.getScriptRam("grow.js")
	var growTimeMs = ns.getGrowTime(target);
	var ramHack = ns.getScriptRam("hack.js")
	var hackTimeMs = ns.getHackTime(target)
	var maxRam = ns.getServerMaxRam(hostname)
	var usedRam = ns.getServerUsedRam(hostname)
	var maxThreadsWeaken = Math.floor(maxRam / ramWeaken)
	var maxThreadsGrow = Math.floor(maxRam / ramGrow)
	var maxThreadsHack = Math.floor(maxRam / ramHack)

	var minSecurityLevel = ns.getServerMinSecurityLevel(target)
	var securityThresh = Math.ceil(minSecurityLevel * 1.1);

	var maxMoney = ns.getServerMaxMoney(target)
	var moneyThresh = maxMoney * 0.975;
	var maxMoneyK = (maxMoney % 1000000) / 1000;
	var maxMoneyMil = Math.trunc(maxMoney / 1000000);

	var availbleMoney = ns.getServerMoneyAvailable(target);
	var availbleMoneyK = (availbleMoney % 1000000) / 1000;
	var availbleMoneyMil = Math.trunc(availbleMoney / 1000000);

	var serverSecLevel = ns.getServerSecurityLevel(target);
	ns.print("Money: ", availbleMoneyMil, "m, ", availbleMoneyK, "k",
		"\nMaxMoney: ", maxMoneyMil, "m, ", maxMoneyK, "k.",
		"\nSecLevel: ", serverSecLevel, ", MinSecLevel: ", minSecurityLevel);

	var waitTime = 0;
	var hacksInSequence = 0
	var maxHacksInSequence = 0;
	var lastMode = "hack"

	var weakenHackThreadsRatio = 25
	var weakenGrowThreadsRatio = 2
	var hackEqulizer = 5;

	while (true) {
		serverSecLevel = ns.getServerSecurityLevel(target)
		availbleMoney = ns.getServerMoneyAvailable(target)

		if (serverSecLevel > securityThresh) {
			ns.print("\nEnter weaken mode.");
			// check which number of threads of weaken() are required to lower the SecLevel to the minimum.
			for (var threads = 1; threads < maxThreadsWeaken; ++threads) {
				var secDecrease = ns.weakenAnalyze(threads, cores);
				//ns.printf(secDecrease);
				var newSecLevel = serverSecLevel - secDecrease;
				if (newSecLevel < minSecurityLevel) {
					break;
				}
				var foundThreads = threads
			}
			//ns.print("Try running ", threads, " weaken threads to decrease the SecLevel to ", newSecLevel, ".")
			usedRam = ns.getServerUsedRam(hostname)
			while ((maxRam - usedRam) < (ramWeaken * threads)) {
				ns.print("Not enough RAM. Reducing threads by 1%");
				threads = Math.floor(threads * 0.99);

				if (threads == 0) {
					ns.print("No RAM avaible, waiting.")
					await ns.sleep(10000)
					threads = foundThreads
				}
				usedRam = ns.getServerUsedRam(hostname)
			}

			// execute weaken and wait for it to finish
			newSecLevel = serverSecLevel - ns.weakenAnalyze(threads, cores);
			ns.print("Weaken x ", threads, " (Decrease SecLevel to ", newSecLevel, ").");
			ns.run("weaken.js", threads, target, iteration);
			while (ns.isRunning("weaken.js", hostname, target, iteration)) { await ns.sleep(10000); }
			lastMode = "weaken"

			// ------------------------------------------------------------------------------------------------------
		} else if (availbleMoney < moneyThresh) {
			ns.print("\nEnter grow mode.");
			// if there is zero money run grow with max threads.
			if (availbleMoney == 0) {
				threads = maxThreadsGrow
			} else {
				// calculate by how much the current money would have to grow to reach the max money
				var growthFactor = maxMoney / availbleMoney;
				threads = Math.ceil(ns.growthAnalyze(target, growthFactor, cores))
				if (threads > maxThreadsGrow) {
					threads = maxThreadsGrow
				}
				if (threads == 0) {
					threads = 1
				}
			}
			// reduce threads if it would make the server too hard to hack
			var secIncrease = ns.growthAnalyzeSecurity(threads, target, cores);
			while (serverSecLevel + secIncrease > 75) {
				ns.print("Would increase sec above 95. Reducing by 1%");
				threads = Math.floor(threads * 0.99);
				var secIncrease = ns.growthAnalyzeSecurity(threads, target, cores);
			}
			usedRam = ns.getServerUsedRam(hostname)
			while ((maxRam - usedRam) < (ramGrow * threads)) {
				ns.print("Not enough RAM. Reducing threads by 1%");
				threads = Math.floor(threads * 0.99);
				if (threads == 0) {
					ns.print("No RAM avaible, waiting.")
					await ns.sleep(10000)
					threads = Math.floor(ns.growthAnalyze(target, growthFactor, cores))
					if (threads > maxThreadsGrow) {
						threads = maxThreadsGrow;
					}
					if (threads == 0) {
						threads = 1;
					}
				}
				usedRam = ns.getServerUsedRam(hostname)
			}


			growTimeMs = ns.getGrowTime(target);
			threadsWeaken = Math.ceil(threads / 10);
			weakenTimeMs = ns.getWeakenTime(target);
			waitTime = weakenTimeMs - growTimeMs - 200;
			ns.print("Weaken x ", threadsWeaken, " (Finish after grow).");
			ns.run("weaken.js", threadsWeaken, target, iteration);
			if (waitTime > 0) {
				ns.print("wait for ", waitTime / 1000, " seconds.");
				await ns.sleep(waitTime);
			}

			usedRam = ns.getServerUsedRam(hostname);
			while ((maxRam - usedRam) < (ramGrow * threads)) {
				ns.print("Not enough RAM. Reducing threads by 1%");
				threads = Math.floor(threads * 0.99);
				usedRam = ns.getServerUsedRam(hostname);
			}
			// run grow sript and wait for it to finish.
			ns.print("Grow x ", threads, "  Increase money by ", growthFactor, " x");
			ns.run("grow.js", threads, target, iteration);

			while (ns.isRunning("grow.js", hostname, target, iteration) || ns.isRunning("weaken.js", hostname, target, iteration)) {
				await ns.sleep(5000);
			}
			ns.print("Grow completed");
			lastMode = "grow"

			// ------------------------------------------------------------------------------------------------------
		} else {
			ns.print("\nEnter hack mode.");

			if (lastMode == "grow") {
				//moneyRatio = moneyRatio - 0.01
				//growthRatio = 1 / (1 - moneyRatio)
				hackEqulizer++;
				hacksInSequence = 0;
				moneyRatio = moneyRatio - 0.025

			} else if (lastMode == "weaken") {
				// starts at 25 (for every 25 hack threads run 1 weaken thread )
				weakenHackThreadsRatio = weakenHackThreadsRatio * 0.99
				// starts at 2 (for every 2 grow threads run 1 weaken thread)
				weakenGrowThreadsRatio = weakenGrowThreadsRatio * 1.01

				hacksInSequence = 0;
			} else {
				hacksInSequence++;
				if (hacksInSequence > maxHacksInSequence) {
					maxHacksInSequence = hacksInSequence;
					if (moneyRatio < 95) {
						moneyRatio = moneyRatio + 0.05
					}
				}
				ns.print(hacksInSequence, " times in a row. (Record: ", maxHacksInSequence, ")")
			}


			// calculate threads needed to get moneyRatio
			var hackThreads = 1;
			if (ns.fileExists("Formulas.exe", "home")) {
				var player = ns.getPlayer()
				var server = ns.getServer(target);
				hackThreads = Math.ceil(moneyRatio / ns.formulas.hacking.hackPercent(server, player))
			} else {
				hackThreads = Math.ceil(moneyRatio / ns.hackAnalyze(target))
			}

			if (hackThreads > maxThreadsHack) {
				ns.print("Too many hack threads")
				hackThreads = maxThreadsHack;
			}

			// check if sec would be increased too much.
			secIncrease = ns.hackAnalyzeSecurity(hackThreads, target);
			while (serverSecLevel + secIncrease > 75) {
				ns.print("Would increase sec above 75. Reducing by 1%");
				hackThreads = Math.floor(hackThreads * 0.99);
				secIncrease = ns.hackAnalyzeSecurity(hackThreads, target);
			}

			// calculate needed threads to counter hack.
			growthFactor = maxMoney / (availbleMoney - (availbleMoney * moneyRatio));

			var growThreads = 1;
			if (ns.fileExists("Formulas.exe", "home")) {
				var player = ns.getPlayer()
				var server = ns.getServer(target);
				server.moneyAvailable = (availbleMoney - (availbleMoney * moneyRatio))
				for (let i = 1; i < maxThreadsGrow; i++) {
					var percentGrow = ns.formulas.hacking.growPercent(server, i, player, cores);
					if (percentGrow > growthFactor*1.01) {
						growThreads = i + 1
						break;
					}
				}
			
			}
			else {
				growThreads = Math.ceil(ns.growthAnalyze(target, growthFactor, cores));
			}


			growThreads = growThreads + hackEqulizer
			var threadsWeakenHack = Math.ceil(hackThreads / weakenHackThreadsRatio); //25
			// calculate needed threads to counter grow.
			var threadsWeakenGrow = Math.ceil(growThreads / weakenGrowThreadsRatio); // 2

			var ramCost = (ramHack * hackThreads) + (ramGrow * growThreads) + (ramWeaken * threadsWeakenHack) + (ramWeaken * threadsWeakenGrow)

			usedRam = ns.getServerUsedRam(hostname)
			var ramReduced = false
			while ((maxRam - usedRam) < ramCost) {
				ns.print("Not enough RAM. Reducing threads by 5%");
				hackThreads = Math.floor(hackThreads * 0.95);
				threadsWeakenHack = Math.ceil(threadsWeakenHack * 0.95);
				growThreads = Math.floor(growThreads * 0.95);
				threadsWeakenHack = Math.ceil(threadsWeakenGrow * 0.95);
				ramCost = (ramHack * hackThreads) + (ramGrow * growThreads) + (ramWeaken * threadsWeakenHack) + (ramWeaken * threadsWeakenGrow)
				usedRam = ns.getServerUsedRam(hostname)
				ramReduced = true
			}
			if (ramReduced == true) { growthFactor = growthFactor * 0.9 }

			growTimeMs = ns.getGrowTime(target);
			hackTimeMs = ns.getHackTime(target)
			weakenTimeMs = ns.getWeakenTime(target);

			ns.print("Start batch, RAM: ", Math.trunc(ramCost), "Gb, Time:", Math.trunc(weakenTimeMs / 1000), "s.");

			ns.print("Weaken x ", threadsWeakenHack, " (Finish after hack).");
			ns.run("weaken.js", threadsWeakenHack, target, iteration, "1");
			await ns.sleep(sleepDelay * 2)
			ns.print("Weaken x ", threadsWeakenGrow, " (Finish after grow).");
			ns.run("weaken.js", threadsWeakenGrow, target, iteration, "2");

			// wait before grow 
			waitTime = weakenTimeMs - growTimeMs - sleepDelay;
			if (waitTime > 0) {
				ns.print("Wait for ", waitTime / 1000, " seconds.");
				await ns.sleep(waitTime)
			}
			usedRam = ns.getServerUsedRam(hostname)
			while ((maxRam - usedRam) < (ramGrow * growThreads)) {
				ns.print("Not enough RAM. Reducing threads by 1%");
				growThreads = Math.floor(growThreads * 0.99);
			}
			ns.print("Grow x ", growThreads, " Increase money by ", growthFactor, " times.",
				"\n Initial ratio was: ", growthRatio);
			ns.run("grow.js", growThreads, target, iteration);

			waitTime = growTimeMs - hackTimeMs - 2 * sleepDelay;
			if (waitTime > 0) {
				ns.print("Wait for ", waitTime / 1000, " seconds.");
				await ns.sleep(waitTime)
			}
			usedRam = ns.getServerUsedRam(hostname)
			while ((maxRam - usedRam) < (ramHack * threads)) {
				ns.print("Not enough RAM. Reducing threads by 1%");
				hackThreads = Math.floor(hackThreads * 0.99);
			}
			ns.print("Hack x ", hackThreads, " (Get ", moneyRatio, " of ", Math.trunc(availbleMoney), ").");
			ns.run("hack.js", hackThreads, target, iteration);

			while (ns.isRunning("hack.js", hostname, target, iteration) || ns.isRunning("weaken.js", hostname, target, iteration, "1") || ns.isRunning("weaken.js", hostname, target, iteration, "2") || ns.isRunning("grow.js", hostname, target, iteration)) {
				await ns.sleep(3000);
			}
			lastMode = "hack"
		}

		availbleMoney = ns.getServerMoneyAvailable(target);
		serverSecLevel = ns.getServerSecurityLevel(target);

		maxMoneyK = (maxMoney % 1000000) / 1000;
		maxMoneyMil = Math.trunc(maxMoney / 1000000);
		availbleMoneyK = (availbleMoney % 1000000) / 1000;
		availbleMoneyMil = Math.trunc(availbleMoney / 1000000);
		ns.print("Money: ", availbleMoneyMil, "m, ", availbleMoneyK, "k.",
			"\nMaxMoney: ", maxMoneyMil, "m, ", maxMoneyK, "k.",
			"\nSecLevel: ", serverSecLevel, ", MinSecLevel: ", minSecurityLevel);

		await ns.sleep(100);
		//ns.print("Go again")
	}
}