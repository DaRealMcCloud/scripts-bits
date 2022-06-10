/** @param {NS} ns */

export async function weakenToMin(ns, securityLevel, minSecurityLevel) {
	ns.tprint("Weaken")
}

export async function growtoMax(ns, money, maxMoney) {
	ns.tprint("Grow")
}

export async function hackAll(ns) {
	ns.tprint("Hack")
}

export async function main(ns) {
	ns.disableLog("sleep")
	var target = ns.args[0];

	var hostname = ns.getHostname()
	var cores = ns.getServer(hostname).cpuCores;

	// caculate how much ram each script takes
	var ramWeaken = ns.getScriptRam("weaken.js")
	var ramGrow = ns.getScriptRam("grow.js")
	var ramHack = ns.getScriptRam("hack.js")
	var maxRam = ns.getServerMaxRam(hostname)
	var maxThreadsWeaken = Math.floor(maxRam / ramWeaken)
	var maxThreadsGrow = Math.floor(maxRam / ramGrow)
	var maxThreadsHack = Math.floor(maxRam / ramHack)

	var minSecurityLevel = ns.getServerMinSecurityLevel(target)
	var securityLevel = ns.getServerSecurityLevel(target)

	var maxMoney = ns.getServerMaxMoney(target)
	var money = ns.getServerMoneyAvailable(target)

	while (true) {
		var weakenTime = await weakenToMin(ns, securityLevel, minSecurityLevel)
		securityLevel = minSecurityLevel

		//wait if needed

		var growTime = await growtoMax(ns, money, maxMoney)
		money = maxMoney

		//wait if needed

		var weakenTime = await weakenToMin(ns, securityLevel, minSecurityLevel)
		securityLevel = minSecurityLevel

		//wait if needed

		var hackTime = await hackAll(ns)
		money = maxMoney
	}

}