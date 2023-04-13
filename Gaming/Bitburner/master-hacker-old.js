/** @param {NS} ns */
export async function main(ns) {
	ns.disableLog("ALL")
	var target = ns.args[0];

	var hostname = ns.getHostname()
	var cores = ns.getServer(hostname).cpuCores;

	// caculate how much ram each script takes
	var ramWeaken = ns.getScriptRam("weaken.js")
	var ramGrow = ns.getScriptRam("grow.js")
	var ramHack = ns.getScriptRam("hack.js")
	var maxRam = ns.getServerMaxRam(hostname)
	var usedRam = ns.getServerUsedRam(hostname)
	var maxThreadsWeaken = Math.floor(maxRam / ramWeaken)
	var maxThreadsGrow = Math.floor(maxRam / ramGrow)
	var maxThreadsHack = Math.floor(maxRam / ramHack)

	var minSecurityLevel = ns.getServerMinSecurityLevel(target)
	var securityThresh = Math.ceil(minSecurityLevel * 1.1);

	var maxMoney = ns.getServerMaxMoney(target)
	var moneyThresh = maxMoney * 0.975;
	

	while (true) {
		var serverSecLevel = ns.getServerSecurityLevel(target)
		var availbleMoney = ns.getServerMoneyAvailable(target)

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
			ns.print("Try running ", threads, " weaken threads to decrease the SecLevel to ", newSecLevel, ".")
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
			ns.print("Running ", threads, " weaken threads to decrease the SecLevel to ", newSecLevel, ".");
			ns.run("weaken.js", threads, target);
			while (ns.isRunning("weaken.js", hostname, target)) { await ns.sleep(10000); }
			availbleMoney = ns.getServerMoneyAvailable(target);
			serverSecLevel = ns.getServerSecurityLevel(target);
			ns.print("Money: ", availbleMoney,", SecLevel: ",serverSecLevel);

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

			// run grow sript and wait for it to finish.
			ns.print("Running ", threads, " grow threads to increase the money by ", growthFactor, ".");
			ns.run("grow.js", threads, target);
			while (ns.isRunning("grow.js", hostname, target)) { await ns.sleep(10000); }
			availbleMoney = ns.getServerMoneyAvailable(target);
			serverSecLevel = ns.getServerSecurityLevel(target);
			ns.print("Money: ", availbleMoney,", SecLevel: ",serverSecLevel);

			// ------------------------------------------------------------------------------------------------------
		} else {
			// calculate threads needed to get 95% of the money
			threads = Math.ceil(0.9 / ns.hackAnalyze(target))
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

			// run hack and wait for it to finish
			availbleMoney = ns.getServerMoneyAvailable(target)
			ns.print("Running ", threads, " hack threads to get 90% of ", availbleMoney,".");
			ns.run("hack.js", threads, target);
			while (ns.isRunning("hack.js", hostname, target)) { await ns.sleep(10000); }
			availbleMoney = ns.getServerMoneyAvailable(target);
			serverSecLevel = ns.getServerSecurityLevel(target);
			ns.print("Money: ", availbleMoney,", SecLevel: ",serverSecLevel);

		}

		await ns.sleep(1000);
		//ns.print("Go again")
	}
}