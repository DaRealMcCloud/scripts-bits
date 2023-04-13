/** @param {NS} ns */
export async function main(ns) {
	ns.disableLog("ALL")
	var target = ns.args[0];

	var hostname = ns.getHostname()
	var cores = ns.getServer(hostname).cpuCores;

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

	var availbleMoney = ns.getServerMoneyAvailable(target);
	var serverSecLevel = ns.getServerSecurityLevel(target);
	ns.print("Money: ", availbleMoney, ", MaxMoney: ", maxMoney, ", SecLevel: ", serverSecLevel, ", MinSecLevel: ", minSecurityLevel);

	var waitTime = 0;

	while (true) {
		serverSecLevel = ns.getServerSecurityLevel(target)
		availbleMoney = ns.getServerMoneyAvailable(target)

		if (serverSecLevel > securityThresh) {
			// check which number of threads of weaken() are required to lower the SecLevel to the minimum.
			for (var threads = 1; threads < maxThreadsWeaken; ++threads) {
				var secDecrease = ns.weakenAnalyze(threads, cores);
				//ns.printf(secDecrease);
				var newSecLevel = serverSecLevel - ns.weakenAnalyze(threads, cores);
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
			ns.run("weaken.js", threads, target);
			while (ns.isRunning("weaken.js", hostname, target)) { await ns.sleep(10000); }

			// ------------------------------------------------------------------------------------------------------
		} else if (availbleMoney < moneyThresh) {
			// if there is zero money run grow with max threads.
			if (availbleMoney == 0) {
				threads = maxThreadsGrow
			} else {
				// calculate by how much the current money would have to grow to reach the max money
				var growthFactor = Math.ceil(maxMoney / availbleMoney);
				threads = Math.floor(ns.growthAnalyze(target, growthFactor, cores))
				if (threads > maxThreadsGrow) {
					threads = maxThreadsGrow
				}
				if (threads == 0) {
					threads = 1
				}
			}
			// reduce threads if it would make the server too hard to hack
			var secIncrease = ns.growthAnalyzeSecurity(threads, target, cores);
			serverSecLevel = ns.getServerSecurityLevel(target)
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
			ns.run("weaken.js", threadsWeaken, target);
			if (waitTime > 0) {
				ns.print("wait for ", waitTime / 1000, " seconds.");
				await ns.sleep(waitTime);
			}

			// run grow sript and wait for it to finish.
			ns.print("Grow x ", threads, "  Increase money by ", growthFactor, " x");
			ns.run("grow.js", threads, target);

			while (ns.isRunning("grow.js", hostname, target) || ns.isRunning("weaken.js", hostname, target)) {
				await ns.sleep(5000);
			}

			// ------------------------------------------------------------------------------------------------------
		} else {
			// calculate threads needed to get 95% of the money
			threads = Math.ceil(0.95 / ns.hackAnalyze(target))
			if (threads > maxThreadsHack) {
				threads = maxThreadsHack;
			}
			secIncrease = ns.hackAnalyzeSecurity(threads, target);
			serverSecLevel = ns.getServerSecurityLevel(target)
			while (serverSecLevel + secIncrease > 75) {
				ns.print("Would increase sec above 75. Reducing by 1%");
				threads = Math.floor(threads * 0.99);
				secIncrease = ns.hackAnalyzeSecurity(threads, target);
			}
			usedRam = ns.getServerUsedRam(hostname)
			while ((maxRam - usedRam) < (ramHack * threads)) {
				ns.print("Not enough RAM. Reducing threads by 1%");
				threads = Math.floor(threads * 0.99);

				if (threads == 0) {
					ns.print("No RAM avaible, waiting.")
					await ns.sleep(10000)
					threads = threads = Math.ceil(0.95 / ns.hackAnalyze(target))
					if (threads > maxThreadsGrow) {
						threads = maxThreadsGrow;
					}
				}
				usedRam = ns.getServerUsedRam(hostname)
			}

			var growThread = Math.floor(ns.growthAnalyze(target, 23, cores));
			var threadsWeakenHack = Math.ceil(threads / 25);
			var threadsWeakenGrow = Math.ceil(growThread / 2);


			ns.print("Weaken x ", threadsWeakenHack, " (Finish after hack).");
			ns.run("weaken.js", threadsWeakenHack, target, "1");
			await ns.sleep(sleepDelay * 2)
			ns.print("Weaken x ", threadsWeakenGrow, " (Finish after grow).");
			ns.run("weaken.js", threadsWeakenGrow, target, "2");

			growTimeMs = ns.getGrowTime(target);
			weakenTimeMs = ns.getWeakenTime(target);
			waitTime = weakenTimeMs - growTimeMs - sleepDelay;
			if (waitTime > 0) {
				ns.print("Wait for ", waitTime / 1000, " seconds.");
				await ns.sleep(waitTime)
			}

			ns.print("Grow x ", growThread, " Increase money by 22 x");
			ns.run("grow.js", growThread, target);

			hackTimeMs = ns.getHackTime(target)
			weakenTimeMs = ns.getWeakenTime(target);
			waitTime = growTimeMs - hackTimeMs - 2 * sleepDelay;
			if (waitTime > 0) {
				ns.print("Wait for ", waitTime / 1000, " seconds.");
				await ns.sleep(waitTime)
			}

			// run hack and wait for it to finish
			availbleMoney = ns.getServerMoneyAvailable(target)
			ns.print("Hack x ", threads, " (Get 95% of ", availbleMoney, ").");
			ns.run("hack.js", threads, target);

			while (ns.isRunning("hack.js", hostname, target) || ns.isRunning("weaken.js", hostname, target) || ns.isRunning("grow.js", hostname, target)) {
				await ns.sleep(3000);
			}
		}

		availbleMoney = ns.getServerMoneyAvailable(target);
		serverSecLevel = ns.getServerSecurityLevel(target);
		ns.print("Money: ", availbleMoney, ", SecLevel: ", serverSecLevel);

		await ns.sleep(100);
		//ns.print("Go again")
	}
}