/** @param {NS} ns */
export async function main(ns) {
	var target = ns.args[0];
	
	await ns.scp("controler.js", target);
	await ns.scp("multi-threader.js", target);
	await ns.scp("weaken.js", target);
	await ns.scp("grow.js", target);
	await ns.scp("hack.js", target);
	ns.exec("controler.js", target, 1, "multi-threader.js");
}