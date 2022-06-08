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

	var minSecurityLevel = ns.getServerMinSecurityLevel(target);
	var securityThresh = Math.ceil(minSecurityLevel * 1.1);

	var secIncreaseHack = 30


	var maxMoney = ns.getServerMaxMoney(target);
	var moneyThresh = maxMoney * 0.975;

	availbleMoney = ns.getServerMoneyAvailable(target);
	serverSecLevel = ns.getServerSecurityLevel(target);
	ns.print("Money: ", availbleMoney, ", SecLevel: ", serverSecLevel);
	ns.print("MaxMoney: ", maxMoney, ", MinSecLevel: ", minSecurityLevel);


	while (true) {
		var serverSecLevel = ns.getServerSecurityLevel(target);
		var availbleMoney = ns.getServerMoneyAvailable(target);

		var weakenTimeMs = ns.getWeakenTime(target);
		var growTimeMs = ns.getGrowTime(target);
		var hackTimeMs = ns.getHackTime(target);

		var weakenTimeS = weakenTimeMs/1000;
		var growTimeS = growTimeMs/1000;
		var hackTimeS = hackTimeMs/1000;

		ns.print("Weakentime: ", weakenTimeS, ", Growtime: ", growTimeS, ", Hacktime: ", hackTimeS)

		// weaken ------------------------------------------------------------------------------------------------------
		// check which number of threads of weaken() are required to lower the SecLevel to the minimum.

		if (availbleMoney == 0) {
			threads = maxThreadsGrow;
		} else {
			// calculate by how much the current money would have to grow to reach the max money
			var growthFactor = Math.ceil(maxMoney / availbleMoney);
			threads = Math.floor(ns.growthAnalyze(target, growthFactor, cores))
			if (threads > maxThreadsGrow) {
				threads = maxThreadsGrow;
			}
			if (threads == 0) {
				threads = 1;
			}
		}
		// reduce threads if it would make the server too hard to hack
		var secIncreaseGrow = ns.growthAnalyzeSecurity(threads, target, cores);
		serverSecLevel = minSecurityLevel;
		while (serverSecLevel + secIncreaseGrow > 75) {
			ns.print("Would increase sec above 95. Reducing by 1%");
			threads = Math.floor(threads * 0.99);
			secIncreaseGrow = ns.growthAnalyzeSecurity(threads, target, cores);
		}
		usedRam = ns.getServerUsedRam(hostname);
		while ((maxRam - usedRam) < (ramGrow * threads)) {
			ns.print("Not enough RAM. Reducing threads by 1%");
			threads = Math.floor(threads * 0.99);
			if (threads == 0) {
				ns.print("No RAM avaible, waiting.")
				await ns.sleep(10000);
				threads = Math.floor(ns.growthAnalyze(target, growthFactor, cores))
				if (threads > maxThreadsGrow) {
					threads = maxThreadsGrow;
				}
				if (threads == 0) {
					threads = 1;
				}
			}
			usedRam = ns.getServerUsedRam(hostname);
		}
		secIncreaseGrow = ns.growthAnalyzeSecurity(threads, target, cores);

		// run grow sript and wait for it to finish.
		growTimeMs = ns.getGrowTime(target) * threads;
		growTimeS = growTimeMs/1000;

		ns.print("Running ", threads, " grow threads to increase the money by ", growthFactor, ".");
		ns.print("Should take ", growTimeS, " seconds.")
		ns.run("grow.js", threads, target);

		// ------------------------------------------------------------------------------------------------------


		serverSecLevel = minSecurityLevel + secIncreaseGrow
		for (var threads = 1; threads < maxThreadsWeaken; ++threads) {
			var secDecrease = ns.weakenAnalyze(threads, cores);
			//ns.printf(secDecrease);
			var newSecLevel = serverSecLevel - secDecrease;
			if (newSecLevel < minSecurityLevel) {
				break;
			}
			var foundThreads = threads;
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
		weakenTimeMs = ns.getWeakenTime(target) * threads;
		weakenTimeS = weakenTimeMs/1000;
		var waitTime = growTimeMs - weakenTimeMs
		if (waitTime > 0) {
			ns.print("wait for ", waitTime / 1000, " seconds.");
			await ns.sleep(waitTime + 50);
		}

		ns.print("Running ", threads, " weaken threads to decrease the SecLevel to ", newSecLevel, ".");
		ns.print("Should take ", weakenTimeS, " seconds.")
		ns.run("weaken.js", threads, target);

		// ------------------------------------------------------------------------------------------------------




		// calculate threads needed to get 95% of the money
		threads = Math.ceil(0.9 / ns.hackAnalyze(target))
		if (threads > maxThreadsHack) {
			threads = maxThreadsHack;
		}
		serverSecLevel = minSecurityLevel + secIncreaseGrow //from grow
		secIncreaseHack = ns.hackAnalyzeSecurity(threads, target);
		while (serverSecLevel + secIncreaseHack > 75) {
			ns.print("Would increase sec above 75. Reducing by 1%");
			threads = Math.floor(threads * 0.99);
			secIncreaseHack = ns.hackAnalyzeSecurity(threads, target);
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
		hackTimeMs = ns.getHackTime(target) * threads;
		hackTimeS = hackTimeMs/1000;



		serverSecLevel = minSecurityLevel + secIncreaseHack
		ns.print("New calculated sec level: ", serverSecLevel)
		for (var threads = 1; threads < maxThreadsWeaken; ++threads) {
			var secDecrease = ns.weakenAnalyze(threads, cores);
			//ns.printf(secDecrease);
			var newSecLevel = serverSecLevel - secDecrease;
			if (newSecLevel < minSecurityLevel) {
				break;
			}
			foundThreads = threads;
		}
		ns.print("Try running second ", threads, " weaken threads to decrease the SecLevel to ", newSecLevel, ".")
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
		weakenTimeMs = ns.getWeakenTime(target) * threads;
		weakenTimeS = weakenTimeMs/1000;
		ns.print("Running second ", threads, " weaken threads to decrease the SecLevel to ", newSecLevel, ".");
		ns.print("Should take ", weakenTimeS, " seconds.")
		ns.run("weaken2.js", threads, target);

		// ------------------------------------------------------------------------------------------------------



		// run hack and wait for it to finish

		waitTime = weakenTimeMs - hackTimeMs
		if (waitTime > 0) {
			ns.print("wait for ", waitTime / 1000, " seconds.");
			await ns.sleep(waitTime + 100)
		}
		ns.print("Running ", threads, " hack threads to get 90% of ", maxMoney, ".");
		ns.print("Should take ", hackTimeS, " seconds.")
		ns.run("hack.js", threads, target);


		// To prevent the script from crashing/terminating after closing and restarting the game.
		while (ns.isRunning("grow.js", hostname, target) || ns.isRunning("weaken.js", hostname, target) || ns.isRunning("hack.js", hostname, target)) {
			await ns.sleep(10000);
		}

		availbleMoney = ns.getServerMoneyAvailable(target);
		serverSecLevel = ns.getServerSecurityLevel(target);
		ns.print("Money: ", availbleMoney, ", SecLevel: ", serverSecLevel);

		await ns.sleep(10000);
		//ns.print("Go again")
	}
}