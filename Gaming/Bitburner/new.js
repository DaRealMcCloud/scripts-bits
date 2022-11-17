/** @param {NS} ns */
export async function main(ns) {
	ns.disableLog("ALL")
	var target = ns.args[0];
	var iteration = ns.args[1];

	var hostname = ns.getHostname()
	var cores = ns.getServer(hostname).cpuCores;

	var moneyRatio = 0.3
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
	var notEnoughRam = false;

	var minSecurityLevel = ns.getServerMinSecurityLevel(target)
	var securityThresh = Math.ceil(minSecurityLevel * 1.1);

	var maxMoney = ns.getServerMaxMoney(target)
	var moneyThresh = maxMoney * 0.95;
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

	var enoughRam = true;

	while (true) {
		if (lastMode == "grow") {
			//we have grown too much last time.
		}


		serverSecLevel = ns.getServerSecurityLevel(target)
		availbleMoney = ns.getServerMoneyAvailable(target)

		if (serverSecLevel > securityThresh) {
			ns.print("\nEnter weaken mode.");
			// check which number of threads of weaken() are required to lower the SecLevel to the minimum.
			for (var threads = 1; threads < maxThreadsWeaken; ++threads) {
				//ns.print("Check what happens with this many threads: ", threads);

				var newSecLevel = serverSecLevel - ns.weakenAnalyze(threads, cores);
				if (newSecLevel < minSecurityLevel) {
					var foundThreads = threads
					break;
				}
				//await ns.sleep(50);
			}
			ns.print("Try running ", threads, " weaken threads to decrease the SecLevel to ", Math.round(newSecLevel * 10) / 10, ".")
			notEnoughRam = false;
			usedRam = ns.getServerUsedRam(hostname)
			while ((maxRam - usedRam) < (ramWeaken * threads)) {
				//ns.print("Not enough RAM. Reducing ", threads, " threads by 1%");
				notEnoughRam = true;
				threads = Math.floor(threads * 0.95);
				usedRam = ns.getServerUsedRam(hostname)

				if (threads < 1) {
					threads = 1;
					break;
				}
				//await ns.sleep(50);
			}

			if (notEnoughRam == true) {
				ns.print("Not enough RAM, reduced threads to  ", threads, ".");
				notEnoughRam = false;
			}

			// execute weaken and wait for it to finish
			newSecLevel = serverSecLevel - ns.weakenAnalyze(threads, cores);
			ns.print("Weaken x ", threads, " (Decrease SecLevel to ", Math.round(newSecLevel * 10) / 10, ").");
			ns.run("weaken.js", threads, target, iteration);
			await ns.sleep(weakenTimeMs);
			while (ns.isRunning("weaken.js", hostname, target, iteration)) {
				ns.print("Waiting for weaken to finish.")
				await ns.sleep(10000);
			}
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
				if (growthFactor < 1) {
					growthFactor = 1;
					ns.print("Growth factor under 0. MaxMoney:", maxMoney, " , Money: ", availbleMoney, " , Money Ratio: ", moneyRatio);
				}
				ns.print("grow factor: ", Math.round(growthFactor * 10) / 10);
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
				threads = Math.ceil(threads * 0.99);
				var secIncrease = ns.growthAnalyzeSecurity(threads, target, cores);
				await ns.sleep(200);
			}
			notEnoughRam = false;
			usedRam = ns.getServerUsedRam(hostname)
			while ((maxRam - usedRam) < (ramGrow * threads)) {
				//ns.print("Not enough RAM. Reducing ", threads, " threads by 1%");
				notEnoughRam = true;
				threads = Math.floor(threads * 0.95);
				if (threads == 0) {
					ns.print("No RAM avaible, waiting.")
					await ns.sleep(10000)

					threads = Math.ceil(ns.growthAnalyze(target, growthFactor, cores))
					if (threads > maxThreadsGrow) {
						threads = maxThreadsGrow;
					}
					if (threads < 1) {
						threads = 1;
						break;
					}
				}
				usedRam = ns.getServerUsedRam(hostname);
			}
			if (notEnoughRam == true) {
				ns.print("Not enough RAM, reduced threads to  ", threads, ".");
				notEnoughRam = false;
			}



			threadsWeaken = Math.ceil(threads / weakenGrowThreadsRatio);
			if (threadsWeaken < 1) { threadsWeaken = 1; }



			weakenTimeMs = ns.getWeakenTime(target);
			growTimeMs = ns.getGrowTime(target);
			waitTime = weakenTimeMs - growTimeMs - 200;
			ns.print("Weaken x ", threadsWeaken, " (Finish after grow).");
			ns.run("weaken.js", threadsWeaken, target, iteration);
			if (waitTime > 0) {
				ns.print("wait for ", waitTime / 1000, " seconds.");
				await ns.sleep(waitTime);
			}

			notEnoughRam = false;
			usedRam = ns.getServerUsedRam(hostname);
			while ((maxRam - usedRam) < (ramGrow * threads)) {
				//ns.print("Not enough RAM. Reducing ", threads, " threads by 1%");
				notEnoughRam = true;
				threads = Math.floor(threads * 0.95);
				usedRam = ns.getServerUsedRam(hostname);
				//await ns.sleep(1000);
				if (threads < 1) {
					threads = 1;
					break;
				}
			}

			if (notEnoughRam == true) {
				ns.print("Not enough RAM, reduced threads to  ", threads, ".");
				notEnoughRam = false;
			}

			var checkThreads = Math.ceil(ns.growthAnalyze(target, 1, cores))

			for (let i = 1.001; checkThreads < threads; i = i + 0.001) {
				//ns.print("Check new growth rate i");
				checkThreads = Math.ceil(ns.growthAnalyze(target, i, cores));
				growthFactor = i;
				//await ns.sleep(20);
			}

			// run grow sript and wait for it to finish.
			ns.print("Grow x ", threads, "  Increase money by ", growthFactor, " x");
			ns.run("grow.js", threads, target, iteration);

			await ns.sleep(growTimeMs);

			var scriptsAreRunning = true
			while (scriptsAreRunning == true) {
				if (ns.isRunning("grow.js", hostname, target, iteration)) {
					ns.print("Waiting for grow to finish.")
					await ns.sleep(5000);
				} else if (ns.isRunning("weaken.js", hostname, target, iteration)) {
					ns.print("Waiting for weaken to finish.")
					await ns.sleep(5000);
				} else {
					scriptsAreRunning = false
				}
				await ns.sleep(1000);
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
				if (moneyRatio - 0.025 > 0.001) {
					moneyRatio = moneyRatio - 0.025
				}

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
					if ((moneyRatio + 0.05) < 95 && enoughRam == true) {
						ns.print("Increase money grab.")
						moneyRatio = moneyRatio * 1.1
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

			ns.print("Need ", hackThreads, " hack threads to get ", moneyRatio)

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
				await ns.sleep(50);
			}

			// calculate needed threads to counter hack.
			growthFactor = maxMoney / (availbleMoney - (availbleMoney * moneyRatio));

			if (growthFactor < 1) {
				growthFactor = 1;
				ns.print("Growth factor under 0. MaxMoney:", maxMoney, " , Money: ", availbleMoney, " , Money Ratio: ", moneyRatio);
			}

			var growThreads = 1;
			if (ns.fileExists("Formulas.exe", "home") == true) {
				ns.print("Use formulars.")
				var player = ns.getPlayer()
				var server = ns.getServer(target);
				server.moneyAvailable = (availbleMoney - (availbleMoney * moneyRatio))
				for (let i = 1; i < maxThreadsGrow; i++) {
					var percentGrow = ns.formulas.hacking.growPercent(server, i, player, cores);
					if (percentGrow > (growthFactor * 1.01)) {
						growThreads = i + 1
						break;
					}
					//await ns.sleep(1000);
				}
			}
			else {
				ns.print("grow factor:", growthFactor, " max money:", maxMoney, "money:", availbleMoney);
				growThreads = Math.ceil(ns.growthAnalyze(target, growthFactor, cores));
			}
			//var secIncreaseGrow = ns.growthAnalyzeSecurity(growThreads, target, cores);

			ns.print("Calculated grow threads :", growThreads)
			growThreads = growThreads + hackEqulizer
			var threadsWeakenHack = Math.ceil(hackThreads / weakenHackThreadsRatio); //25
			// calculate needed threads to counter grow.
			var threadsWeakenGrow = Math.ceil(growThreads / weakenGrowThreadsRatio); // 2




			var ramCost = (ramHack * hackThreads) + (ramGrow * growThreads) + (ramWeaken * threadsWeakenHack) + (ramWeaken * threadsWeakenGrow)

			usedRam = ns.getServerUsedRam(hostname)
			var ramReduced = false
			while ((maxRam - usedRam) < ramCost) {
				//ns.print("Not enough RAM. Reducing ", hackThreads ," hack threads by 10%");
				//ns.print("Not enough RAM. Reducing ", threadsWeakenHack ," weakenhack threads by 5%");
				//ns.print("Not enough RAM. Reducing ", growThreads ," grow threads by 10%");
				//ns.print("Not enough RAM. Reducing ", threadsWeakenGrow ," weaken grow threads by 5%");
				hackThreads = Math.floor(hackThreads * 0.75);
				threadsWeakenHack = Math.floor(threadsWeakenHack * 0.75);
				growThreads = Math.ceil(growThreads * 0.75);
				threadsWeakenGrow = Math.floor(threadsWeakenGrow * 0.75);
				ramCost = (ramHack * hackThreads) + (ramGrow * growThreads) + (ramWeaken * threadsWeakenHack) + (ramWeaken * threadsWeakenGrow)
				usedRam = ns.getServerUsedRam(hostname)
				ramReduced = true
				await ns.sleep(50);
			}
			if (ramReduced == true) {
				//growthFactor = growthFactor * 0.9 
				moneyRatio = moneyRatio * 0.66;
				/*
				if((moneyRatio-0.1) > 0.2) {
					moneyRatio = moneyRatio - 0.1
				} else if((moneyRatio-0.01) > 0.1) {
					moneyRatio = moneyRatio - 0.01
				} else if((moneyRatio-0.001) > 0.01) {
					moneyRatio = moneyRatio - 0.001
				} else {
					ns.print("Not reducing under 1 %.")
				}
				*/
				ns.print("Not enough RAM. Reducing threads and money ratio to ", moneyRatio);

				enoughRam = false
			} else {
				enoughRam = true
			}

			growTimeMs = ns.getGrowTime(target);
			hackTimeMs = ns.getHackTime(target)


			if (ns.fileExists("Formulas.exe", "home")) {
				player = ns.getPlayer()

				server = ns.getServer(target);
				//server.hackDifficulty = server.hackDifficulty+secIncrease
				weakenTimeMs = ns.formulas.hacking.weakenTime(server, player)

				server = ns.getServer(target);
				//server.hackDifficulty = server.hackDifficulty+secIncreaseGrow
				var weakenTimeGrowMs = ns.formulas.hacking.weakenTime(server, player)
			}
			else {
				weakenTimeMs = ns.getWeakenTime(target);
				var weakenTimeGrowMs = ns.getWeakenTime(target);
			}


			ns.print("Start batch, RAM: ", Math.trunc(ramCost), "Gb, Time:", Math.trunc(weakenTimeMs / 1000), "s.");

			if (threadsWeakenHack < 1) {
				threadsWeakenHack = 1;
				ns.print("threadsWeakenHack had to be reduced to 1.");
			}
			ns.print("Weaken x ", threadsWeakenHack, " (Finish after hack).");
			ns.run("weaken.js", threadsWeakenHack, target, iteration, "1");
			await ns.sleep(sleepDelay * 2)
			if (threadsWeakenGrow < 1) {
				threadsWeakenGrow = 1;
				ns.print("threadsWeakenHack had to be reduced to 1.");
			}
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
				ns.print("Not enough RAM. Reducing ", growThreads, " threads by 1%");
				growThreads = Math.floor(growThreads * 0.95);
				//await ns.sleep(1000);
				if (growThreads < 1) {
					growThreads = 1;
					ns.print("GrowThreads had to be reduced to 1.");
					break;
				}
			}

			ns.print("Grow x ", growThreads, " Increase money by ", growthFactor, " times.",
				"\n Initial ratio was: ", growthRatio);
			ns.run("grow.js", growThreads, target, iteration);

			waitTime = growTimeMs - hackTimeMs - 2 * sleepDelay;
			if (waitTime > 0) {
				ns.print("Wait for ", waitTime / 1000, "s before hack.");
				await ns.sleep(waitTime)
			}
			notEnoughRam = false;
			usedRam = ns.getServerUsedRam(hostname)
			while ((maxRam - usedRam) < (ramHack * threads)) {
				ns.print("Not enough RAM. Reducing ", threads, " threads by 1%");
				notEnoughRam = true;
				hackThreads = Math.floor(hackThreads * 0.99);
				//await ns.sleep(1000);
				if (hackThreads < 1) {
					hackThreads = 1;
					ns.print("Hackthreads had to be reduced to 1.");
					moneyRatio = moneyRatio * 0.66;
					break;
				}
			}
			if (notEnoughRam == true) {
				ns.print("Not enough RAM, reduced threads to  ", threads, ".");
				notEnoughRam = false;
			}

			ns.print("Hack x ", hackThreads, " (Get ", moneyRatio, " of ", Math.trunc(availbleMoney), "). \nThats ", Math.trunc((moneyRatio * availbleMoney) / (1000 * 1000)), "m.");
			ns.run("hack.js", hackThreads, target, iteration);
			await ns.sleep(hackTimeMs + 40)
			while (ns.isRunning("hack.js", hostname, target, iteration) || ns.isRunning("weaken.js", hostname, target, iteration, "1") || ns.isRunning("weaken.js", hostname, target, iteration, "2") || ns.isRunning("grow.js", hostname, target, iteration)) {
				ns.print("Waiting for hack to finish.")
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

		await ns.sleep(1000);
		//ns.print("Go again")
	}
}