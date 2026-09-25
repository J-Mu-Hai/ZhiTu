export interface AccountProfile {
  id: string;
  name: string;
  email: string;
  passwordDigest: string;
  school: string;
  major: string;
  year: string;
  rank: number;
  targetYear: number;
  targetGoal: string;
  bio: string;
  createdAt: string;
}

export interface RegisterAccountInput {
  name: string;
  email: string;
  password: string;
  school: string;
  major: string;
  year: string;
  targetYear: number;
  targetGoal: string;
}

export type EditableProfile = Pick<
  AccountProfile,
  'name' | 'school' | 'major' | 'year' | 'rank' | 'targetYear' | 'targetGoal' | 'bio'
>;
