export const Roles={admin:'材料负责人',analyst:'光谱研究员',viewer:'修复阅读者'} as const;
export const RunStatus={queued:'等待解混',running:'计算中',completed:'已完成',failed:'失败',blocked:'已停止发布'} as const;
export const ReviewStatus={submitted:'待审阅',approved:'已批准',rejected:'退回复配'} as const;
export const PublishState={active:'有效',revoked:'来源撤销 / 停止发布',superseded:'已被替代'} as const;
export const SourceStatus={active:'有效',revoked:'已撤销'} as const;
export const SourceKind={measured:'实测谱',replacement:'替代谱'} as const;
export const ReplacementState={none:'无需替代',pending:'等待替代',queued:'替代已排队',running:'替代计算中',completed:'替代已完成',failed:'替代失败',blocked:'替代受阻'} as const;
