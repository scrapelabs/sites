import { Router, type IRouter } from "express";
import healthRouter from "./health";
import proxiesRouter from "./proxies";
import usecasesRouter from "./usecases";
import leadsRouter from "./leads";
import adminRouter from "./admin";
import supportRouter from "./support";

const router: IRouter = Router();

router.use(healthRouter);
router.use(proxiesRouter);
router.use(usecasesRouter);
router.use(leadsRouter);
router.use(adminRouter);
router.use(supportRouter);

export default router;
